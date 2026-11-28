"""The live runner conversation's two reads (#89), in the task's own project: /<slug>/tasks/<id>/run/activity (history by seq) and
.../activity/stream (SSE: new frames as they arrive). Read-only; nothing here writes.

Access is the task's: project_from_path (the project must be one the signed-in person may VIEW; anyone else gets the same 404 as a missing one)
and the task must belong to that project. The client portal and every client-visible view are other routers and never include this: the content
is what an agent read and ran in the repository, so it is for people who work the project, whatever the task's client_visible flag says.
Frames are untrusted text; the JSON carries them as plain strings and the page renders them escaped (static/js/activity.js)."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app import access, job_activity, jobs, web_auth
from app.db import get_cursor
from app.routers.pages import ProjectDep, _task_in

project_router = APIRouter(prefix="/{slug}")

STREAM_POLL_SECONDS = 2.0            # the longest a stream waits for a wake-up (job_activity.accept wakes it at once)
STREAM_HEARTBEAT_SECONDS = 15.0
STREAM_AFTER_END_SECONDS = 6.0       # keep reading this long after the job ended: the runner's last batch can land just after the state flips
JOB_RECHECK_SECONDS = 10.0           # the job row is read again this often (state, a new job); not on every wake
ACCESS_RECHECK_SECONDS = 15.0        # the session and the person's access are checked again this often
MAX_STREAMS_PER_USER = 6
_open: dict = {}                     # open streams by person
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _job_for(task_id: int, job_id: int = 0) -> dict | None:
    """The task's current job, or (job_id given) THAT job if it is one of this task's: so an older run can be opened from a timeline."""
    if not job_id:
        return jobs.latest_for_task(task_id)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, state FROM jobs WHERE id = %s AND task_id = %s", (job_id, task_id))
        row = cur.fetchone()
    return dict(row) if row else None


def _after(raw) -> int:
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 0


def _results_allowed(who, project: dict) -> bool:
    """Tool OUTPUT (file contents, command output) only for people who can run jobs or edit the project; a viewer sees messages and tool-call rows."""
    return access.can_edit(who, project["id"])


@project_router.get("/tasks/{task_id}/run/activity")
def activity_history(request: Request, task_id: int, after: int = 0, limit: int = 500, job: int = 0, project: dict = ProjectDep):
    """The task's current job's frames after `after` (a seq; 0 = from the start), at most `limit` (<= 1000). `last_seq` is the cursor to continue from."""
    _task_in(project, task_id)
    job = _job_for(task_id, job)
    if not job:
        return JSONResponse({"job_id": None, "state": "", "live": False, "frames": [], "last_seq": _after(after), "session_url": ""}, headers=HEADERS)
    frames = job_activity.for_viewer(job_activity.history(job["id"], _after(after), limit), _results_allowed(access.of_request(request), project))
    return JSONResponse({"job_id": job["id"], "state": job["state"], "live": job["state"] in jobs.LIVE_STATES, "frames": frames,
                         "last_seq": frames[-1]["seq"] if frames else _after(after), "session_url": job_activity.session_url(job["id"])},
                        headers=HEADERS)


def _sse(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()


def _recheck(request: Request, project_id: int) -> tuple[bool, bool]:
    """(may still view the project, may still see tool output): the session and the person's access, read again (a sign-out, a disabled user, a
    removed share end the stream)."""
    who = access.of_web_user(web_auth.resolve_session(web_auth.cookie_from_headers(request.scope["headers"], web_auth.session_cookie_name()))
                             if web_auth.required() else None)
    return access.can_view(who, project_id), access.can_edit(who, project_id)


async def _stream(request: Request, task_id: int, after: int, want: int, project_id: int, can_results: bool, release):
    try:
        yield b"retry: 3000\n\n"
        cursor, last_beat, ended_at, job_id, job, job_at, access_at = after, time.monotonic(), None, None, None, 0.0, time.monotonic()
        watch = None
        try:
            while True:
                if await request.is_disconnected():
                    return
                now = time.monotonic()
                if now - access_at >= ACCESS_RECHECK_SECONDS:
                    access_at = now
                    try:
                        ok, can_results = await asyncio.to_thread(_recheck, request, project_id)
                    except Exception:  # noqa: BLE001 - a DB hiccup: keep the stream, look again next time
                        ok = True
                    if not ok:
                        yield _sse("end", "access")
                        return
                try:
                    if job is None or job_at == 0.0 or now - job_at >= JOB_RECHECK_SECONDS:
                        fresh = await asyncio.to_thread(_job_for, task_id, want)
                        job_at = now
                        if fresh and job_id is not None and fresh["id"] != job_id and not want:   # a new job on the task: tell the window to reload the thread
                            yield _sse("reset", str(fresh["id"]))
                            return
                        job = fresh or job
                        job_id = job["id"] if job else job_id
                        if job and watch is None:
                            watch = job_activity.Watch(job["id"]).__enter__()
                    frames = await asyncio.to_thread(job_activity.history, job["id"], cursor, 200) if job else []
                except Exception:  # noqa: BLE001 - a DB hiccup: skip this tick, keep the stream
                    await asyncio.sleep(STREAM_POLL_SECONDS)
                    continue
                if frames:
                    cursor = frames[-1]["seq"]
                    last_beat = time.monotonic()
                    shown = job_activity.for_viewer(frames, can_results)
                    yield _sse("frames", json.dumps({"frames": shown, "last_seq": cursor, "state": job["state"]}, separators=(",", ":")))
                    continue                                               # more may be waiting: no wait
                if job and job["state"] not in jobs.LIVE_STATES:
                    ended_at = ended_at or time.monotonic()
                    if time.monotonic() - ended_at >= STREAM_AFTER_END_SECONDS:
                        yield _sse("end", job["state"])
                        return
                    job_at = 0.0                                           # the end is near: look at the job each tick until the last batch has landed
                if time.monotonic() - last_beat >= STREAM_HEARTBEAT_SECONDS:
                    yield b": ping\n\n"
                    last_beat = time.monotonic()
                if watch is not None:
                    if await watch.wait(STREAM_POLL_SECONDS):             # woken the moment accept() stores frames (or the job ends); at most this long otherwise
                        job_at = 0.0
                else:
                    await asyncio.sleep(STREAM_POLL_SECONDS)
        finally:
            if watch is not None:
                watch.__exit__(None, None, None)
    finally:
        release()


@project_router.get("/tasks/{task_id}/run/activity/stream")
async def activity_stream(request: Request, task_id: int, after: int = 0, job: int = 0, project: dict = ProjectDep):
    """Server-Sent Events: `frames` ({frames, last_seq, state}) as they are stored, `end` once the job has finished (or access ended), `reset` if the
    task got a new job. At most MAX_STREAMS_PER_USER at once for one person (429 beyond)."""
    await asyncio.to_thread(_task_in, project, task_id)
    who = access.of_request(request)
    key = who.user_id if who.user_id is not None else "owner"
    if _open.get(key, 0) >= MAX_STREAMS_PER_USER:
        return JSONResponse({"detail": "too many live views open: close one"}, status_code=429, headers=HEADERS)
    _open[key] = _open.get(key, 0) + 1
    done = []

    def release():
        if not done:
            done.append(1)
            _open[key] = max(_open.get(key, 1) - 1, 0)
            if not _open[key]:
                _open.pop(key, None)

    return StreamingResponse(_stream(request, task_id, _after(after), job, project["id"], _results_allowed(who, project), release),
                             media_type="text/event-stream", headers={**HEADERS, "Connection": "keep-alive", "X-Accel-Buffering": "no"},
                             background=BackgroundTask(release))
