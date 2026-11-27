"""`crewboss-runner tray` / `crewboss-runner-tray` (#974): a system-tray icon for the runner on this PC (Windows only).

The icon colour is the runner's state, read every ~3 s from its local files (never from the network): grey = stopped, green = idle and
connected, blue = a job is running, amber = needs attention (not enrolled, revoked, disconnected, a health check failing).
Menu: a status line, Start, Stop, Open control panel, Server (only with 2+ profiles, #975), Quit tray. Start and Stop work whichever way the runner is set up (the hidden
logon task, the Windows service, a LaunchAgent / systemd unit, or nothing: then Start runs `crewboss-runnerw run` detached).

The control panel is served by THIS process on 127.0.0.1 (so it works while the runner is stopped). It is opened through a short-lived
launcher file in the user-only state folder that redirects to the page's one-use link: the one-use key is never on a command line
(a process list on Linux shows every user's command lines), only the path of that file is."""
from __future__ import annotations

import html
import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser

from . import appctl, panel, runlog, store
from .ui import StatusServer

log = logging.getLogger("crewboss-runner")
POLL_SECONDS = 3.0
DOCTOR_EVERY = 300.0
RGB = {"grey": (138, 133, 124), "green": (63, 143, 91), "blue": (59, 111, 209), "amber": (208, 138, 26)}
LAUNCHER = "tray-open.html"
TRAY_PID = "tray.pid"


TERRACOTTA = (192, 90, 51)   # app/static/brand/crewboss/mark-tile.svg: #c05a33
CREAM = (251, 250, 247)
MARK_DOTS = ((32, 15.4, 8), (20.5, 34.1, 6), (43.5, 34.1, 6), (9, 50.5, 6), (55, 50.5, 6))   # the triangle of dots, in the svg's 64 box
MARK_SCALE = 5.59327   # the svg draws that box at this scale, centred on a 512 tile


def make_icon(colour: str, image_mod, size: int = 128):
    """The Crewboss mark (the triangle of dots on a terracotta rounded square, as in mark-tile.svg) with a state-coloured badge in the
    corner. Drawn at 4x and reduced, so the dots have smooth edges."""
    from PIL import ImageDraw   # Pillow is the tray's own dependency
    big = size * 4
    k = big / 512
    img = image_mod.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=112.64 * k, fill=TERRACOTTA + (255,))
    for cx, cy, r in MARK_DOTS:
        x, y, rr = (256 + (cx - 32) * MARK_SCALE) * k, (256 + (cy - 32) * MARK_SCALE) * k, r * MARK_SCALE * k
        d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=CREAM + (255,))
    r = big * 0.2   # the state badge, top right (the corner with no dot), ringed in white so it reads on the tile
    cx, cy = big - r - big * 0.03, r + big * 0.03
    d.ellipse((cx - r - big * 0.04, cy - r - big * 0.04, cx + r + big * 0.04, cy + r + big * 0.04), fill=(255, 255, 255, 255))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=RGB[colour] + (255,))
    return img.resize((size, size), getattr(image_mod, "LANCZOS", 1))


class Tray:
    """All the tray's behaviour, with pystray, Pillow and the OS calls injected so it is testable without a desktop."""

    def __init__(self, pystray, image_mod, ctl=appctl, opener=webbrowser.open, make_server=None, doctor_check=None, clock=time.monotonic,
                 confirm=None):
        self.pystray, self.image_mod, self.ctl, self.opener = pystray, image_mod, ctl, opener
        self.make_server = make_server or (lambda: StatusServer(on_exchange=self._remove_launcher, actions=panel.Actions(on_removed=self.on_quit)).start())
        self.doctor_check = doctor_check or (lambda: panel.Health().get(fresh=True)["failing"])
        self.clock = clock
        self.confirm = confirm or _confirm_box
        self.server = None
        self.state: dict = {"colour": "grey", "status": "Starting...", "running": False, "job": None, "enrolled": True}
        self.doctor_failing, self._doctor_at = 0, -DOCTOR_EVERY
        self.icon = None
        self._stop = threading.Event()
        self._icons: dict = {}

    # --- state -> icon -----------------------------------------------------------------------------------------------
    def refresh(self) -> dict:
        """Poll the local state once and update the icon and the menu."""
        if self.clock() - self._doctor_at >= DOCTOR_EVERY:
            self._doctor_at = self.clock()
            threading.Thread(target=self._doctor_bg, daemon=True).start()
        try:
            self.state = panel.local_state(self.doctor_failing)
        except Exception:  # noqa: BLE001  a broken read must not kill the tray
            log.exception("tray: could not read the runner state")
            return self.state
        if self.icon is not None:
            colour = self.state["colour"]
            if colour not in self._icons:
                self._icons[colour] = make_icon(colour, self.image_mod)
            self.icon.icon = self._icons[colour]
            self.icon.title = ("Crewboss runner: " + self.state["status"])[:127]   # Windows truncates a tooltip at 127
            self.icon.update_menu()
        return self.state

    def _doctor_bg(self) -> None:
        try:
            self.doctor_failing = int(self.doctor_check())
        except Exception:  # noqa: BLE001
            self.doctor_failing = 0

    # --- the menu ----------------------------------------------------------------------------------------------------
    def status_text(self, item=None) -> str:
        return self.state["status"]

    def menu(self):
        P = self.pystray
        return P.Menu(
            P.MenuItem(self.status_text, None, enabled=False),
            P.Menu.SEPARATOR,
            P.MenuItem("Start", self.on_start, enabled=lambda item: not self.state["running"] and self.state["enrolled"]),
            P.MenuItem("Stop", self.on_stop, enabled=lambda item: self.state["running"]),
            P.MenuItem("Open control panel", self.on_open, default=True),
            P.MenuItem("Server", P.Menu(self.server_items), visible=lambda item: len(self.state.get("profiles") or []) > 1),
            P.Menu.SEPARATOR,
            P.MenuItem("Quit tray", self.on_quit))

    def server_items(self):
        """The Server submenu (#975): one radio item per profile, the active one ticked. Built fresh each time the menu opens."""
        P = self.pystray
        for p in self.state.get("profiles") or []:
            label = p["server"] + (f" ({p['name']})" if p["name"] != p["server"] else "") + ("" if p["state"] == "enrolled" else f" - {p['state']}")
            yield P.MenuItem(label, self._switcher(p["name"], p["server"]), checked=lambda item, a=p["active"]: a, radio=True)

    def _switcher(self, name: str, server: str):
        return lambda icon=None, item=None: self._bg(self._switch, name, server)

    def _switch(self, name: str, server: str) -> None:
        if any(p["name"] == name and p["active"] for p in self.state.get("profiles") or []):
            return
        force = False
        if self.state.get("job"):   # a menu has no dialog of its own: ask in a message box
            if not self.confirm(f"A job is running. Switching to {server} now cancels it and it goes back to the queue.\n\nSwitch anyway?"):
                return
            force = True
        ok, msg = self.ctl.use_profile(name, force=force)
        self._notify(msg if not ok else f"Now using {server}")
        self.refresh()

    def _bg(self, fn, *a) -> None:
        threading.Thread(target=fn, args=a, daemon=True).start()

    def on_start(self, icon=None, item=None) -> None:
        self._bg(self._start)

    def _start(self) -> None:
        ok, msg = self.ctl.start()
        self._notify("Starting the runner" if ok else "Could not start the runner: " + msg)
        self.refresh()

    def on_stop(self, icon=None, item=None) -> None:
        if self.state.get("job"):   # the confirm lives in the control panel: a menu has no dialog
            self._notify("A job is running. Confirm the stop in the control panel.")
            self.on_open()
            return
        self._bg(self._stop_runner)

    def _stop_runner(self) -> None:
        ok, msg = self.ctl.stop()
        self._notify("Runner stopped" if ok else "Could not stop the runner: " + msg)
        self.refresh()

    def on_open(self, icon=None, item=None) -> None:
        self._bg(self.open_panel)

    def on_quit(self, icon=None, item=None) -> None:
        self._stop.set()
        if self.server is not None:
            self.server.stop()
            self.server = None
        self._remove_launcher()
        _drop_pid()
        if self.icon is not None:
            self.icon.stop()

    def _notify(self, text: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(text, "Crewboss runner")
        except Exception:  # noqa: BLE001  not every desktop supports balloons
            pass

    # --- opening the page --------------------------------------------------------------------------------------------
    def launcher_path(self):
        return store.state_dir() / LAUNCHER

    def open_panel(self) -> str:
        """Start the panel server if needed, mint a fresh one-use link, and open it through the launcher file. Returns the file's path."""
        if self.server is None:
            self.server = self.make_server()
        link = self.server.new_link()
        page = ("<!doctype html><meta charset=utf-8><title>Opening Crewboss runner</title>"
                f"<script>location.replace({json.dumps(link)})</script><noscript><a href=\"{html.escape(link)}\">Open the control panel</a></noscript>")
        path = self.launcher_path()
        store.write_private(path, page)
        self.opener(path.as_uri())
        threading.Timer(60.0, self._remove_launcher).start()   # a key that nobody used should not sit on disk
        return str(path)

    def _remove_launcher(self) -> None:
        try:
            self.launcher_path().unlink()
        except OSError:
            pass

    # --- run ---------------------------------------------------------------------------------------------------------
    def run(self) -> None:
        P = self.pystray
        self.refresh()
        self._icons[self.state["colour"]] = make_icon(self.state["colour"], self.image_mod)
        self.icon = P.Icon("crewboss-runner", self._icons[self.state["colour"]], "Crewboss runner", self.menu())

        def loop(icon) -> None:
            icon.visible = True
            while not self._stop.wait(POLL_SECONDS):
                self.refresh()
        self.icon.run(setup=lambda icon: threading.Thread(target=loop, args=(icon,), daemon=True).start())


def _confirm_box(text: str) -> bool:
    """A Yes/No box on Windows (the tray is Windows-only); anything else says no."""
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(0, text, "Crewboss runner", 0x4 | 0x30 | 0x40000) == 6   # MB_YESNO | MB_ICONWARNING | MB_TOPMOST -> IDYES
    except Exception:  # noqa: BLE001
        return False


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _other_tray_running() -> bool:
    try:
        return _pid_alive(int((store.state_dir() / TRAY_PID).read_text().strip()))
    except (OSError, ValueError):
        return False


def _drop_pid() -> None:
    try:
        (store.state_dir() / TRAY_PID).unlink()
    except OSError:
        pass


NOT_WINDOWS = ("The tray is a Windows feature. On Linux and macOS the runner is command-line first: crewboss-runner run | status | doctor | "
               "service install. The control panel page still works: run `crewboss-runner status` for its link while the runner is running.")


def cmd_tray(args=None) -> int:
    if sys.platform != "win32":
        print(NOT_WINDOWS, file=sys.stderr)
        return 1
    try:
        import pystray
        from PIL import Image
    except ImportError as exc:
        print(f"The tray needs pystray and Pillow ({exc}). Install them with: pip install pystray Pillow", file=sys.stderr)
        return 1
    if _other_tray_running():
        print("The tray is already running on this PC.", file=sys.stderr)
        return 0
    store.mark_dir(store.state_dir())
    store.write_private(store.state_dir() / TRAY_PID, str(os.getpid()))
    try:
        Tray(pystray, Image).run()
    except Exception as exc:  # noqa: BLE001  e.g. no desktop session / no AppIndicator on Linux
        print(f"The tray could not start ({exc.__class__.__name__}: {exc}).", file=sys.stderr)
        _drop_pid()
        return 1
    _drop_pid()
    return 0


def main(argv: list[str] | None = None) -> int:
    """The `crewboss-runner-tray` GUI-subsystem script: no console window on Windows."""
    runlog.setup(name="tray.log")   # no console: log to a file, and never crash on a bare print()
    return cmd_tray()


if __name__ == "__main__":
    sys.exit(main())
