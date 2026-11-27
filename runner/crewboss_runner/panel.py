"""The control panel's logic (#974), apart from HTTP: what the page shows and what its buttons do.

State is read from the runner's own local files (status.json, run.pid, config) so the same panel works from the tray process while the
runner is stopped. Actions go through appctl (the adapter) and only ever take FIXED choices: no path, command or URL comes from a request."""
from __future__ import annotations

import json
import socket
import threading
import time

from . import __version__, appctl, store, trayauto

COLOURS = ("grey", "green", "blue", "amber")


class RateLimit:
    """At most `limit` actions per `window` seconds, overall (a person clicking, not a script)."""

    def __init__(self, limit: int = 20, window: float = 10.0, clock=time.monotonic):
        self.limit, self.window, self.clock, self.hits = limit, window, clock, []

    def allow(self) -> bool:
        now = self.clock()
        self.hits = [t for t in self.hits if now - t < self.window]
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(now)
        return True


_cache: dict = {}
CACHE_TTL = 45.0   # autostart and tray-autostart status ask schtasks / sc / systemctl: never on every 3 s poll


def cached(key: str, fn, tag=None):
    """fn() at most every CACHE_TTL seconds (and again when `tag`, e.g. the elevation state, changes or invalidate() ran)."""
    hit = _cache.get(key)
    if hit and hit[2] == tag and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    val = fn()
    _cache[key] = (time.monotonic(), val, tag)
    return val


def invalidate() -> None:
    _cache.clear()


def read_status_file() -> dict:
    try:
        data = json.loads((store.state_dir() / "status.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def classify(enrolled: bool, running: bool, link: str, job: dict | None, preflight: list, doctor_failures: int = 0) -> tuple[str, str]:
    """(colour, one-line status). grey = stopped, green = idle and connected, blue = a job is running, amber = needs attention."""
    if not enrolled:
        return "amber", "Not enrolled: paste an enrolment code"
    if not running:
        return "grey", "Stopped"
    if link == "revoked":
        return "amber", "Revoked or expired: forget this PC and enrol again"
    if preflight:
        return "amber", "Not claiming jobs: " + str(preflight[0])[:120]
    if link != "ready":
        return "amber", "Connecting..." if link in ("connecting", "starting") else "Disconnected, retrying"
    if job:
        who = " ".join(x for x in (job.get("project"), job.get("title") or f"task #{job.get('task_id', '?')}") if x)
        return "blue", f"Running a job: {who}"[:140]
    if doctor_failures:
        return "amber", f"Idle, but {doctor_failures} health check(s) failing"
    return "green", "Idle and connected"


def local_state(doctor_failures: int = 0, now: float | None = None) -> dict:
    """Everything the page and the tray show. Never a token or key."""
    now = time.time() if now is None else now
    cfg = appctl.enrolled()
    running = appctl.is_running()
    st = read_status_file() if running else {}
    link = st.get("state", "starting") if running else "stopped"
    job = st.get("job") if running else None
    if job:
        job = {**job, "elapsed_s": max(0, int(now - job["started"])) if job.get("started") else None}
    preflight = st.get("preflight") or [] if running else []
    colour, text = classify(bool(cfg), running, link, job, preflight, doctor_failures)
    ka = appctl.keep_awake_info()
    runner_ka = st.get("keep_awake") or {}   # written by the RUNNER: held now, effective setting, env override
    ka["holding"] = bool(runner_ka.get("held")) and running
    if runner_ka and running:
        ka["enabled"], ka["locked_by_env"] = bool(runner_ka.get("enabled", ka["enabled"])), bool(runner_ka.get("env", ka["locked_by_env"]))
    plat = appctl.platform()
    tag = appctl.elevated_status()["state"]
    server = (cfg or {}).get("server") or ""
    scheme = (cfg or {}).get("scheme", "https")
    return {"version": __version__, "platform": plat, "enrolled": bool(cfg), "colour": colour, "status": text,
            "running": running, "link": link, "last_error": st.get("last_error", "") if running else "",
            "profiles": [{"name": p["name"], "server": p["server"], "state": p["state"], "active": p["active"]} for p in appctl.list_profiles()],
            "server": server, "crewboss_url": f"{scheme}://{server}" if server and scheme in ("http", "https") else "",
            "name": (cfg or {}).get("name") or "", "device_id": (cfg or {}).get("device_id"), "mode": (cfg or {}).get("mode") or "",
            "job": job, "preflight": preflight, "recent_jobs": list(reversed(st.get("recent_jobs") or [])) if running else [],
            "events": (st.get("events") or [])[-8:] if running else [],
            "elevated": st.get("elevated") if running else None, "keep_awake": ka, "autostart": {"kind": cached("autostart", appctl.autostart_kind, tag), "options": appctl.autostart_options(plat)},
            "tray_autostart": {"available": trayauto.available(plat), "on": cached("tray", lambda: trayauto.is_enabled(plat), tag)},
            "elevation": appctl.elevated_status(), "runners_page": f"{scheme}://{server}/runners" if server and scheme in ("http", "https") else "", "now": now}


class Health:
    """The doctor's rows and the repairs it can do, cached (doctor starts a few programs)."""

    def __init__(self, ttl: float = 20.0):
        self.ttl, self.at, self.data, self.lock = ttl, 0.0, None, threading.Lock()

    def get(self, fresh: bool = False) -> dict:
        with self.lock:
            if fresh or self.data is None or time.monotonic() - self.at > self.ttl:
                try:
                    rows = appctl.doctor_rows()
                    self.data = {"rows": rows, "repairs": appctl.repairs(), "failing": sum(r["status"] == "fail" for r in rows),
                                 "warning": sum(r["status"] == "warn" for r in rows), "error": ""}
                except Exception as exc:  # noqa: BLE001  a broken check must not break the page
                    self.data = {"rows": [], "repairs": [], "failing": 0, "warning": 0, "error": f"health check failed ({exc.__class__.__name__})"}
                self.at = time.monotonic()
            return self.data


class Actions:
    """name -> (ok, message, extra). Each takes a small dict of FIXED fields and validates them."""

    def __init__(self, health: Health | None = None, on_removed=None):
        self.health = health or Health()
        self.on_removed = on_removed
        self.table = {"start": self.start, "stop": self.stop, "autostart": self.autostart, "keep_awake": self.keep_awake,
                      "tray_autostart": self.tray_autostart, "use_profile": self.use_profile, "repair": self.repair, "enrol": self.enrol, "recheck": self.recheck, "remove": self.remove}

    def run(self, name: str, body: dict) -> tuple[int, dict]:
        fn = self.table.get(name)
        if fn is None:
            return 404, {"detail": "no such action"}
        if not isinstance(body, dict):
            return 400, {"detail": "bad request"}
        return fn(body)

    @staticmethod
    def _res(ok: bool, msg: str, **extra) -> tuple[int, dict]:
        return (200 if ok else 409), {"ok": ok, "message": msg, **extra}

    def start(self, body: dict):
        return self._res(*appctl.start())

    def stop(self, body: dict):
        if appctl.job_running() and body.get("confirm") is not True:
            return 409, {"ok": False, "needs_confirm": True, "message": "A job is running. Stopping now cancels it and it goes back to the queue."}
        return self._res(*appctl.stop(force=body.get("confirm") is True))

    def autostart(self, body: dict):
        mode = body.get("mode")
        if not isinstance(mode, str):
            return 400, {"detail": "bad request"}
        if appctl.job_running() and body.get("confirm") is not True and mode != appctl.autostart_kind():   # same confirm as Stop, on every OS
            return 409, {"ok": False, "needs_confirm": True, "message": "A job is running on this PC."}
        invalidate()
        return self._res(*appctl.set_autostart(mode))

    def keep_awake(self, body: dict):
        if not isinstance(body.get("on"), bool):
            return 400, {"detail": "bad request"}
        return self._res(*appctl.set_keep_awake(body["on"]))

    def tray_autostart(self, body: dict):
        if not isinstance(body.get("on"), bool) or not trayauto.available():
            return 400, {"detail": "bad request"}
        invalidate()
        return self._res(*(trayauto.enable() if body["on"] else trayauto.disable()))

    def use_profile(self, body: dict):
        """Switch the server this PC's runner talks to (#975). Same confirm as Stop when a job runs: switching sends that job back to the queue."""
        name = body.get("name")
        if not isinstance(name, str) or not store.valid_name(name):
            return 400, {"detail": "bad request"}
        if not any(p["name"] == name for p in appctl.list_profiles()):
            return 409, {"ok": False, "message": f"This PC has no server profile called {name}."}
        if appctl.job_running() and body.get("confirm") is not True:
            return 409, {"ok": False, "needs_confirm": True, "message": "A job is running. Switching servers now cancels it and it goes back to the queue."}
        invalidate()
        return self._res(*appctl.use_profile(name, force=body.get("confirm") is True))

    def repair(self, body: dict):
        rid = body.get("id")
        if not isinstance(rid, str) or len(rid) > 20:
            return 400, {"detail": "bad request"}
        ok, msg = appctl.run_repair(rid)
        self.health.get(fresh=True)
        return self._res(ok, msg)

    def recheck(self, body: dict):
        h = self.health.get(fresh=True)
        return 200, {"ok": True, "message": f"{h['failing']} failing, {h['warning']} warning(s)"}

    def enrol(self, body: dict):
        code = body.get("code")
        if appctl.enrolled():
            return 409, {"ok": False, "message": "This PC is already enrolled."}
        if not isinstance(code, str) or not (8 <= len(code.strip()) <= 400):
            return 400, {"detail": "bad request"}
        return self._res(*appctl.enrol(code, socket.gethostname()))

    def remove(self, body: dict):
        """Danger zone. A typed REMOVE is required in the request itself, on top of the CSRF token."""
        if body.get("confirm") != "REMOVE":
            return 400, {"detail": "bad request", "message": "Type REMOVE to confirm."}
        def done() -> None:
            if self.on_removed:
                threading.Timer(2.0, self.on_removed).start()   # only after the removal really finished; the page reads the reply first
        ok, msg = appctl.remove_runner(on_done=done)
        if not ok:
            return 409, {"ok": False, "message": msg or "Could not remove the runner."}
        if msg == "pending":
            return 202, {"ok": True, "pending": True, "message": "Waiting for administrator approval...", "final_command": appctl.FINAL_COMMAND}
        return 200, {"ok": True, "message": "The runner was removed from this PC.", "final_command": appctl.FINAL_COMMAND}
