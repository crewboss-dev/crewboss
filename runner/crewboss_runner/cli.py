"""crewboss-runner: enrol <code> | run | status | tray | doctor | repair | uninstall | forget | service install|uninstall|status."""
from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import json
import logging
import os
import platform
import signal
import socket
import sys
import urllib.parse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import __version__, doctor, elevation, enrol as enrol_mod, keepawake, profiles, runlog, service, stopctl, store, tlspin
from .ca import CaWorker
from .envcheck import EnvWorker
from .link import Link
from .ui import StatusServer
from .worker import Status, Worker


def load_identity(secrets: store.Secrets, config: store.Config):
    cfg = config.load()
    key_b64, token = secrets.get("device_key"), secrets.get("device_token")
    if not cfg or not key_b64 or not token:
        return None
    return cfg, Ed25519PrivateKey.from_private_bytes(base64.b64decode(key_b64)), token


def snapshot(cfg: dict, secrets: store.Secrets, status: Status, link: Link | None) -> dict:
    """Everything the UI and `status` may show. Never a token or key."""
    return {"link": link.state if link else status.data.get("state", "not running"),
            "last_error": link.last_error if link else status.data.get("last_error", ""),
            "device_id": cfg.get("device_id"), "name": cfg.get("name"), "mode": cfg.get("mode"),
            "server": cfg.get("server"), "fp8": cfg.get("fp8"), "secrets_backend": secrets.describe(),
            "job": status.data.get("job"), "preflight": status.data.get("preflight", []), "events": status.data.get("events", []), "version": __version__}


def is_loopback_host(hostport: str) -> bool:
    try:
        host = (urllib.parse.urlsplit("//" + hostport.strip()).hostname or "").lower()   # `localhost:80@evil.example` is evil.example
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cmd_enrol(args) -> int:
    try:
        pname = profiles.default_name_for_enrol(args.name or getattr(args, "profile", None), getattr(args, "replace", False))
    except profiles.ProfileError as exc:
        print(f"enrol refused: {exc}", file=sys.stderr)
        return 1
    if profiles.exists(pname) and not getattr(args, "replace", False):
        print(f"enrol refused: a profile named {pname!r} already exists (server {(store.Config(profile=pname).load() or {}).get('server', '?')}). "
              "Pick another --name, or add --replace to enrol it again (the old device stays on its server until someone revokes it).", file=sys.stderr)
        return 1
    secrets, config = store.Secrets(profile=pname), store.Config(profile=pname)
    if args.insecure_http and not getattr(args, "allow_insecure_remote", False):
        try:
            hostport = enrol_mod.parse_code(args.code)[0]
        except enrol_mod.EnrolError:
            hostport = ""   # enrol() reports the bad code
        if hostport and not is_loopback_host(hostport):
            print("enrol refused: --insecure-http sends everything in clear text and is for a server on this computer "
                  "(localhost). For another host use https, or add --allow-insecure-remote if you accept that anyone on the "
                  "path can read and alter the traffic.", file=sys.stderr)
            return 1
    try:
        cfg = enrol_mod.enrol(args.code, getattr(args, "runner_name", "") or socket.gethostname(), platform.platform(), secrets, config,
                              scheme="http" if args.insecure_http else "https")
    except enrol_mod.EnrolError as exc:
        print(f"enrol failed: {exc}", file=sys.stderr)
        return 1
    had_others = [n for n in profiles.names() if n != pname] or store.legacy_layout_present()
    active = profiles.after_enrol(pname)
    print(f"Enrolled device #{cfg['device_id']} ({cfg['mode'] or 'mode unknown'}) with {cfg['server']} (server key {cfg['fp8']}) as profile {pname!r}.")
    print(f"Key and token are kept in: {secrets.describe()}")
    if cfg.get("tls_pin"):
        tlspin.sync_cert_file(store.mark_dir(store.profile_state_dir(pname)), cfg)   # the file Claude Code trusts (run also re-syncs it)
        print(f"tls: pinned {cfg['tls_pin'][:8]} (this runner trusts only that certificate for {cfg['server']})")
    if had_others and (args.name or getattr(args, "profile", None)):
        print(f"Added a second profile: {pname!r}. (--name is now the PROFILE name; the device's own name on the server is --runner-name.)")
    if active:
        print("Start it with: crewboss-runner run   (at every login: crewboss-runner service install)")
    else:
        print(f"The active profile is still {(store.read_active() or 'default')!r}. To connect this PC to {cfg['server']}: crewboss-runner use {pname}")
    return 0


def cmd_status(args) -> int:
    secrets, config = store.Secrets(), store.Config()
    cfg = config.load()
    if not cfg:
        print("not enrolled (run: crewboss-runner enrol <code>)" if not getattr(args, "profile", None) else f"no profile {args.profile!r} (see: crewboss-runner profiles)")
        return 1
    if store.current_profile():
        n = len(profiles.names())
        print(f"profile:  {store.current_profile()}" + (f"   ({n} on this PC: crewboss-runner profiles)" if n > 1 else ""))
    have = bool(secrets.get("device_key") and secrets.get("device_token"))
    try:
        run_status = json.loads((store.state_dir() / "status.json").read_text())
    except (OSError, ValueError):
        run_status = {}
    print(f"server:   {cfg['server']} (pinned key {cfg['fp8']})")
    if cfg.get("tls_pin"):
        print(f"tls:      pinned {cfg['tls_pin'][:8]}")
    print(f"device:   #{cfg['device_id']} {cfg.get('name', '')} ({cfg.get('mode') or '?'}), token expires {cfg.get('expires_at', '?')}")
    print(f"secrets:  {secrets.describe()}{'' if have else '  [MISSING: re-enrol]'}")
    if os.name != "nt" and not secrets.file_mode_ok():
        print("WARNING:  the secrets file is not 0600")
    if run_status.get("elevated") is not None and store.run_alive():
        print("elevated: " + ("YES (administrator token: its agents inherit it)" if run_status["elevated"] else "no (standard-user token)"))
    print(f"runner:   {run_status.get('state', 'not running')}"
          + (f" ({run_status['last_error']})" if run_status.get("last_error") else ""))
    url = runlog.read_url()
    if url:
        print(f"page:     {url}   (open on this PC only)")
    for problem in run_status.get("preflight") or []:
        print(f"PROBLEM:  {problem} (this runner claims no jobs until it is fixed)")
    job = run_status.get("job")
    print(f"job:      {('#%s on %s (%s)' % (job['id'], job['branch'], job['state'])) if job else 'idle'}")
    return 0


def cmd_run(args) -> int:
    return asyncio.run(run_async(args))


def cmd_profiles(args) -> int:
    print(profiles.format_table(profiles.list_profiles()))
    return 0


def cmd_use(args) -> int:
    try:
        profiles.use_profile(args.name, force=args.force)
    except profiles.ProfileError as exc:
        print(f"use refused: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_profile(args) -> int:
    try:
        if args.action == "rename":
            if not args.new:
                print("usage: crewboss-runner profile rename <old> <new>", file=sys.stderr)
                return 2
            profiles.rename(args.name, args.new)
            print(f"Renamed profile {args.name!r} to {args.new!r}.")
            return 0
        if args.new:
            print("usage: crewboss-runner profile remove <name> [--yes]", file=sys.stderr)
            return 2
        if profiles.exists(args.name) and not args.yes:
            try:
                ok = input(f"Delete profile {args.name!r} (its key, token and state on this PC)? [y/N] ").strip().lower() in ("y", "yes")
            except EOFError:
                ok = False
            if not ok:
                print("Cancelled.")
                return 1
        server = profiles.remove(args.name)
    except profiles.ProfileError as exc:
        print(f"profile {args.action} refused: {exc}", file=sys.stderr)
        return 1
    print(f"Removed profile {args.name!r}. The device still exists on {server or 'its server'} until someone revokes it on that server's Runners page.")
    return 0


def cmd_forget(args) -> int:
    secrets, config = store.Secrets(), store.Config()
    for name in ("device_key", "device_token"):
        secrets.delete(name)
    config.delete()
    print("Removed this PC's device key, token and settings. The device still exists on the dashboard until someone "
          "revokes it on the Runners page.")
    return 0


async def supervised(name: str, make, delay: float = 5.0):
    """Run `make()` (a coroutine function) for as long as the runner lives. If it dies with an error, say so loudly (stderr and
    the log) and start it again after `delay`: the job loop and the assistant's loop must never go quiet while the socket looks fine."""
    while True:
        try:
            return await make()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("crewboss-runner").exception("%s loop crashed", name)
            print(f"ERROR: the {name} loop crashed ({type(exc).__name__}: {exc}); restarting in {delay:.0f}s", file=sys.stderr, flush=True)
            await asyncio.sleep(delay)


async def run_async(args) -> int:
    pin = store.current_profile()   # decided ONCE, here, before run.pid exists (this is also where a legacy enrolment migrates)
    if pin:
        store.set_profile_override(pin)   # pinned for the whole run: stopctl.watch, runlog and the pid cleanup never re-resolve it
    secrets, config = store.Secrets(), store.Config()
    ident = load_identity(secrets, config)
    if not ident:
        print("not enrolled (run: crewboss-runner enrol <code>)", file=sys.stderr)
        return 1
    cfg, key, token = ident
    scheme = cfg.get("scheme", "https")
    state = store.mark_dir(store.state_dir())
    store.write_pid()
    status = Status(state / "status.json")
    status.set(elevated=elevation.is_elevated())   # `status` and `doctor` show whether the runner (and so its agents) has administrator rights
    tlspin.sync_cert_file(state, cfg)   # the file Claude Code and the Gemini MCP trust: it always matches the config
    link = Link(cfg, key, token)
    link.on_tls = lambda: (config.save(cfg), tlspin.sync_cert_file(state, cfg))   # a verified tls_rotate / dropping the old certificate
    link.on_change = lambda: status.set(state=link.state, last_error=link.last_error)
    worker = Worker(link, cfg, state, status, f"{scheme}://{cfg['server']}")
    ca_worker = CaWorker(link, cfg, state, f"{scheme}://{cfg['server']}")   # the assistant's chat turns, beside the jobs (task #702)
    env_worker = EnvWorker(link)                                              # "is this PC ready for that project?" (#810): no agent
    ui = StatusServer(lambda: snapshot(cfg, secrets, status, link), port=args.ui_port, on_rearm=runlog.write_url).start()
    runlog.write_url(ui.url)   # a hidden runner has no console to print it on: `crewboss-runner status` reads it from here
    if runlog.headless_started():   # the log file is not the place for a key: `crewboss-runner status` prints the link
        print("Status page: run `crewboss-runner status` on this PC for the link")
    else:
        print(f"Status page (open on this PC only): {ui.url}")
    print(f"Secrets are kept in: {secrets.describe()}")
    loop = asyncio.get_running_loop()
    stop_link = lambda: loop.call_soon_threadsafe(lambda: asyncio.ensure_future(link.stop()))   # noqa: E731
    if os.name == "nt":   # no loop.add_signal_handler on Windows: Ctrl+C / Ctrl+Break arrive through signal.signal
        def _stop(*_):
            stop_link()
        signal.signal(signal.SIGINT, _stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _stop)
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(link.stop()))
    link_task = asyncio.create_task(link.run())
    stop_watch = asyncio.create_task(stopctl.watch(stop_link))   # `service stop`: a graceful stop with no console to Ctrl+C
    work_task = asyncio.create_task(supervised("job", worker.loop))
    ca_task = asyncio.create_task(supervised("assistant", ca_worker.loop))
    env_task = asyncio.create_task(supervised("environment", env_worker.loop))
    await link_task
    stop_watch.cancel()
    worker.stopping = ca_worker.stopping = env_worker.stopping = True
    work_task.cancel()   # kills a running agent before its worktree goes (Worker.run_job)
    ca_task.cancel()
    env_task.cancel()
    for t in (work_task, ca_task, env_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    keepawake.GLOBAL.release_all()   # shutdown (Ctrl+C, SvcStop, revoke): never leave the PC held
    ui.stop()
    runlog.clear_url()
    store.release_pid()
    if link.state == "revoked":
        print("This runner was revoked or expired. Run: crewboss-runner forget; then enrol again.", file=sys.stderr)
        return 2
    if stopctl.LAST_REASON == "switch":   # `use <profile>`: the service's supervisor restarts the runner at once, on the new profile
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="crewboss-runner")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--profile", metavar="NAME", help="act on this profile instead of the active one (one-off; `run --profile X` runs it in the foreground)")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enrol", help="enrol this PC with a code from the dashboard's Runners page")
    e.add_argument("code")
    e.add_argument("--name", default="", help="the profile to create (default: \"default\" on a PC with no profiles)")
    e.add_argument("--replace", action="store_true", help="enrol an existing profile again")
    e.add_argument("--runner-name", default="", help="the device's name on the server (default: this PC's host name)")
    e.add_argument("--insecure-http", action="store_true", help="plain http/ws (local development only)")
    e.add_argument("--allow-insecure-remote", action="store_true",
                   help="allow --insecure-http for a host that is not this computer (unsafe: clear text on the network)")
    e.set_defaults(fn=cmd_enrol)
    r = sub.add_parser("run", help="hold the socket and run jobs")
    r.add_argument("--ui-port", type=int, default=0, help="local status page port (default: any free port)")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("profiles", help="list the Crewboss servers this PC is enrolled to (* = active)").set_defaults(fn=cmd_profiles)
    u = sub.add_parser("use", help="make a profile the active one and reconnect the runner to its server")
    u.add_argument("name")
    u.add_argument("--force", action="store_true", help="switch even if a job is running (it goes back to the queue)")
    u.set_defaults(fn=cmd_use)
    pr = sub.add_parser("profile", help="profile rename <old> <new> | profile remove <name> [--yes]")
    pr.add_argument("action", choices=("rename", "remove"))
    pr.add_argument("name")
    pr.add_argument("new", nargs="?", default="")
    pr.add_argument("--yes", action="store_true", help="do not ask")
    pr.set_defaults(fn=cmd_profile)
    sub.add_parser("status", help="show enrolment and runner state").set_defaults(fn=cmd_status)
    sub.add_parser("forget", help="delete this PC's key, token and settings").set_defaults(fn=cmd_forget)
    d = sub.add_parser("doctor", help="check this PC: Python, git, Claude Code, keyring, enrolment, autostart, disk, agent setup")
    d.add_argument("--json", action="store_true", help="print the result as JSON")
    d.add_argument("--label", action="append", default=[], help="also check what this runner label needs (repeatable), e.g. android-usb")
    d.set_defaults(fn=doctor.cmd_doctor)
    rp = sub.add_parser("repair", help="fix the safe problems doctor finds (asks before each change)")
    rp.add_argument("--yes", action="store_true", help="do not ask")
    rp.add_argument("--dry-run", action="store_true", help="list what would be changed and change nothing")
    rp.set_defaults(fn=doctor.cmd_repair)
    un = sub.add_parser("uninstall", help="stop and remove the autostart, this PC's key, token, settings and state")
    un.add_argument("--yes", action="store_true", help="do not ask")
    un.add_argument("--all", action="store_true", help="remove EVERY profile and the autostart (default: only the active / --profile one)")
    un.set_defaults(fn=doctor.cmd_uninstall)
    sv = sub.add_parser("service", help="start the runner at login: install | uninstall | status | stop | start")
    sv.add_argument("action", choices=("install", "uninstall", "status", "stop", "start"))
    sv.add_argument("--windows-service", action="store_true",
                    help="Windows, admin: a real Windows service that runs as you (asks for your password) instead of the hidden logon task")
    sv.add_argument("--force", action="store_true", help="stop / uninstall even if a job is running (the job goes back to the queue)")
    sv.add_argument("--dry-run", action="store_true", help="print what would be written and run, and do neither")
    sv.add_argument("--systemd", action="store_true", help="on WSL2: use a systemd user unit instead of a Windows logon task")
    sv.set_defaults(fn=service.main)
    sub.add_parser("tray", help="a system-tray icon: runner state, Start / Stop, and the local control panel (needs pystray and Pillow)"
                   ).set_defaults(fn=lambda a: __import__("crewboss_runner.tray", fromlist=["cmd_tray"]).cmd_tray(a))
    for sp in sub.choices.values():   # `crewboss-runner status --profile X` as well as `--profile X status`
        if not any("--profile" in a.option_strings for a in sp._actions):
            sp.add_argument("--profile", metavar="NAME", default=argparse.SUPPRESS, help="act on this profile instead of the active one")
    args = p.parse_args(argv)
    store.set_profile_override(None)
    store.ensure_migrated()   # a legacy enrolment migrates FIRST, so --profile / the env var never skips it
    try:
        store.set_profile_override(getattr(args, "profile", None))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    runlog.setup()   # a no-op unless there is no console to print to
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
