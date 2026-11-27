"""Server profiles (task #975): one PC enrolled to several Crewboss servers, ONE active at a time.

Layout (every folder user-only):
  <config root>/active                      the name of the active profile
  <config root>/profiles/<name>/config.json + secrets.json (only when there is no OS keyring)
  <state root>/profiles/<name>/             status.json, run.pid, ui-url.txt, stop.request, jobs/, runner.log
  OS keyring service "crewboss-runner/<name>"  (names device_key, device_token)

`store.config_dir()` / `store.state_dir()` return the ACTIVE profile's folders, so the service, doctor, stop and status code all act
on the active profile unchanged. There is ONE autostart (task, service, unit, LaunchAgent); the runner reads the active profile when it
starts. `--profile X` (or CREWBOSS_RUNNER_PROFILE) points one command at another profile.

A runner enrolled before profiles (config.json directly in the config root, secrets in the keyring service "crewboss-runner" or the
older "mallen-runner") becomes the profile "default" on first use, with no re-enrol: see migrate_legacy.

The small API the Windows tray and control panel call: list_profiles(), active_profile(), use_profile(name, force=False)."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from pathlib import Path

from . import store

SECRET_NAMES = ("device_key", "device_token")


class ProfileError(Exception):
    """A message fit to show the person as is."""


class ProfileBusy(ProfileError):
    """A job is running (or the runner would not stop)."""


@contextlib.contextmanager
def as_profile(name: str):
    """Run a block with `store.*` pointed at profile `name`."""
    before = store._override
    store.set_profile_override(name)
    try:
        yield
    finally:
        store._override = before


def check_name(name: str) -> str:
    if not store.valid_name(name):
        raise ProfileError(f"bad profile name {name!r}: use 1-32 of lower-case letters, digits and hyphens (a-z, 0-9, -), not starting with a hyphen")
    return name


def names() -> list[str]:
    root = store.root_config_dir() / store.PROFILES
    try:
        return sorted(d.name for d in root.iterdir() if d.is_dir() and store.valid_name(d.name) and (d / "config.json").is_file())
    except OSError:
        return []


def exists(name: str) -> bool:
    return store.valid_name(name) and (store.profile_config_dir(name) / "config.json").is_file()


def active_profile() -> str | None:
    """The name of the ACTIVE profile (the `active` file), or None when this PC has none (not enrolled). `--profile` does not change it."""
    store.ensure_migrated()
    if store.legacy_layout_present():   # a legacy enrolment still waiting to be migrated is "default"
        return store.DEFAULT_PROFILE
    name = store.read_active() or store.DEFAULT_PROFILE
    return name if exists(name) else None


def set_active(name: str) -> None:
    check_name(name)
    store.write_private(store.root_config_dir() / store.ACTIVE_FILE, name + "\n")
    store.mark_dir(store.root_config_dir())


def _status_of(name: str) -> dict:
    try:
        return json.loads((store.profile_state_dir(name) / "status.json").read_text())
    except (OSError, ValueError):
        return {}


def _alive(name: str) -> bool:
    with as_profile(name):
        return store.run_alive()


def profile_state(name: str) -> str:
    cfg = store.Config(profile=name).load()
    if not cfg:
        return "not enrolled"
    sec = store.Secrets(profile=name)
    if not (sec.get("device_key") and sec.get("device_token")):
        return "missing key"
    if _status_of(name).get("state") == "revoked":
        return "revoked"
    return "enrolled"


def list_profiles() -> list[dict]:
    """[{name, server, runner_name, state, active}], state one of: enrolled, revoked, missing key."""
    act = active_profile()
    out = []
    for n in names():
        cfg = store.Config(profile=n).load() or {}
        out.append({"name": n, "server": cfg.get("server", ""), "runner_name": cfg.get("name", ""),
                    "state": profile_state(n), "active": n == act})
    return out


# ---- migration ----------------------------------------------------------------------------------------------------------
class MigrationBusy(ProfileError):
    """Another process is migrating right now."""


LOCK_NAME = ".migrate.lock"
LOCK_STALE = 120.0   # seconds: a lock older than this whose owner is gone is taken over


@contextlib.contextmanager
def _migration_lock():
    """An exclusive lock file in the root config folder, held for the whole migration."""
    import time
    root = store.ensure_private_dir(store.root_config_dir())
    path = root / LOCK_NAME
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                fresh = time.time() - path.stat().st_mtime < LOCK_STALE and store.run_alive(path)
            except OSError:
                fresh = False
            if fresh:
                raise MigrationBusy("another crewboss-runner is migrating this PC's enrolment; try again in a moment")
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        break
    else:
        raise MigrationBusy("could not take the migration lock")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def _legacy_values(old_sec: store.Secrets) -> dict:
    """Every device secret the legacy enrolment has, from ANY backend: the keyring (both service names) and root secrets.json."""
    vals: dict = {}
    try:
        vals.update({k: v for k, v in json.loads(old_sec.file.read_text()).items() if k in SECRET_NAMES and isinstance(v, str) and v})
    except (OSError, ValueError, AttributeError):
        pass
    kr = store._keyring()
    if kr:
        for n in SECRET_NAMES:
            got = kr.get_password(store.SERVICE, n) or kr.get_password(store.LEGACY_SERVICE, n)
            if got:
                vals[n] = got
    return vals


def _verify(sec: store.Secrets, want: dict) -> bool:
    """Never true for an empty set: nothing proves nothing."""
    return bool(want) and all(sec.get(n) == v for n, v in want.items())


def migrate_legacy() -> bool:
    """Turn the single legacy enrolment into profile "default". Lazy, atomic, idempotent, no re-enrol. True if anything was done.

    Only a user-level process migrates, never while the legacy runner is alive (store._legacy_mode defers it), under an exclusive lock.
    Copy -> verify -> only then remove the old: (1) every secret found in ANY backend (keyring under either service name, and the root
    secrets.json) is gathered; a legacy config with a device_id but no secret anywhere REFUSES the migration; (2) they are written to the
    new keyring service "crewboss-runner/default" and/or profiles/.default.migrating/secrets.json (the root secrets.json is always copied)
    and read back; (3) that folder is renamed to profiles/default (atomic); (4) active = default; (5) the old state moves; (6) after
    verifying again, the old keyring entries, secrets.json and config.json (last: the marker that makes a crash resume here) are removed.
    Any exception leaves the old layout intact and in use."""
    root = store.root_config_dir()
    legacy_cfg = root / "config.json"
    if not legacy_cfg.is_file() or store._no_migrate:
        return False
    with _migration_lock():
        if not legacy_cfg.is_file():   # another process finished while we waited
            return False
        return _migrate_locked(root, legacy_cfg)


def _migrate_locked(root: Path, legacy_cfg: Path) -> bool:
    dest = store.profile_config_dir(store.DEFAULT_PROFILE)
    old_sec = store.Secrets(profile=None)
    want = _legacy_values(old_sec)
    try:
        has_device = bool(json.loads(legacy_cfg.read_text()).get("device_id"))
    except (OSError, ValueError):
        has_device = False
    if not want and has_device and not (dest / "config.json").is_file():
        raise ProfileError("the old enrolment has a device id but its key and token cannot be read from the keyring or secrets.json; "
                           "not migrating (the old layout is kept). Re-enrol if the key is really gone")
    if not (dest / "config.json").is_file():
        tmp = root / store.PROFILES / ".default.migrating"
        shutil.rmtree(tmp, ignore_errors=True)
        store.ensure_private_dir(tmp)
        store.write_private(tmp / "config.json", legacy_cfg.read_text())
        if old_sec.file.is_file():
            store.write_private(tmp / "secrets.json", old_sec.file.read_text())   # always copied, whatever the backend
        new_sec = store.Secrets(cfg=tmp, profile=store.DEFAULT_PROFILE)
        for n, v in want.items():
            if new_sec.backend == "os-keyring":
                new_sec.set(n, v)
            elif new_sec.get(n) != v:
                new_sec.set(n, v)
        if want and not _verify(new_sec, want):
            raise ProfileError("could not verify the copied key and token")
        store.mark_dir(tmp)
        if dest.exists():   # an empty / half folder from an earlier crash
            shutil.rmtree(dest)
        os.replace(tmp, dest)
    else:   # the profile exists (crashed after the rename): make sure its secrets are complete before anything old goes
        new_sec = store.Secrets(cfg=dest, profile=store.DEFAULT_PROFILE)
        for n, v in want.items():
            if not new_sec.get(n):
                new_sec.set(n, v)
    new_sec = store.Secrets(cfg=dest, profile=store.DEFAULT_PROFILE)
    if want and not _verify(new_sec, want):
        raise ProfileError("the migrated key and token did not verify; the old ones were kept")
    store.mark_dir(root)
    if store.read_active() is None:
        set_active(store.DEFAULT_PROFILE)
    _move_legacy_state()
    # verified: now remove the old
    kr = store._keyring()
    if kr:
        for n in SECRET_NAMES:
            for service in (store.SERVICE, store.LEGACY_SERVICE):
                with contextlib.suppress(Exception):
                    kr.delete_password(service, n)
    with contextlib.suppress(OSError):
        old_sec.file.unlink()
    legacy_cfg.unlink()
    return True


def _pid_file_alive(pid_file: Path) -> bool:
    return store.run_alive(pid_file)


def _move_legacy_state() -> None:
    """Best effort: the old state folder's loose files (status, job folders...) go under profiles/default. (The migration is deferred
    while the old runner is alive, so nothing is moved from under a live process; the check stays as a belt.)"""
    sroot = store.root_state_dir()
    if not sroot.is_dir() or _pid_file_alive(sroot / "run.pid"):
        return
    dest = store.mark_dir(store.profile_state_dir(store.DEFAULT_PROFILE))
    for item in sroot.iterdir():
        if item.name in (store.PROFILES, "live.pid", "stop.request"):
            continue
        with contextlib.suppress(OSError, shutil.Error):
            shutil.move(str(item), str(dest / item.name))


# ---- commands -----------------------------------------------------------------------------------------------------------
def default_name_for_enrol(name: str | None, replace: bool = False) -> str:
    """No --name: "default" on a PC with no profiles; otherwise the person must choose."""
    if name:
        return check_name(name)
    if replace and store.DEFAULT_PROFILE in names():
        return store.DEFAULT_PROFILE
    if names():
        raise ProfileError("this PC already has profiles (" + ", ".join(names()) + "); choose a name for the new one: "
                           "crewboss-runner enrol <code> --name <profile>")
    return store.DEFAULT_PROFILE


def after_enrol(name: str) -> bool:
    """The first profile on a PC becomes active. True if `name` is now the active one. While a legacy enrolment is waiting to be
    migrated (its runner is still running) the new profile is NOT made active: the migration makes "default" active."""
    if store.legacy_layout_present():
        return False
    others = [n for n in names() if n != name]
    if not others or store.read_active() not in others:
        set_active(name)
        return True
    return False


def _graceful_stop(reason: str = "") -> bool:
    from . import stopctl
    return stopctl.graceful_stop(reason=reason)


def _supervised_by_service() -> bool:
    """Windows, the real service installed, and this shell is not an administrator: the service's supervisor restarts the runner itself."""
    if os.name != "nt":
        return False
    from . import service
    try:
        return service.windows_service_installed() and not service.is_admin()
    except Exception:  # noqa: BLE001
        return False


def use_profile(name: str, force: bool = False, restart: bool = True, say=print) -> dict:
    """Make `name` the active profile and reconnect the runner to its server. Returns {name, server, restarted}.
    A job running now refuses it unless force (the job goes back to the queue); a runner that will not stop in time refuses too, and the
    active profile is left as it was. All stopping is done against the CURRENT profile's state (pinned), whatever the active file says."""
    check_name(name)
    if not exists(name):
        raise ProfileError(f"no profile {name!r} on this PC (have: {', '.join(names()) or 'none'}). Add it: crewboss-runner enrol <code> --name {name}")
    cfg = store.Config(profile=name).load() or {}
    server = cfg.get("server") or name
    cur = active_profile()
    pin = None if store.legacy_layout_present() else cur   # a legacy enrolment waiting to migrate is looked at through the ROOT state
    if cur == name:
        say(f"{name} is already the active profile.")
        return {"name": name, "server": cfg.get("server", ""), "restarted": False}
    with as_profile(pin) if pin else contextlib.nullcontext():
        running = store.run_alive()
        if running:
            from . import stopctl
            job = stopctl.job_running()
            if job and not force:
                raise ProfileBusy(f"A job is running (#{job.get('id', '?')} on {job.get('branch', '?')}). Switching now cancels it and it goes "
                                  "back to the queue. Wait for it, or add --force.")
    if running and _supervised_by_service():
        # the service's supervisor restarts the runner when it exits with the "switch" code: make the new profile active FIRST so the
        # restarted runner reads it, then ask the old runner to stop
        set_active(name)
        with as_profile(pin) if pin else contextlib.nullcontext():
            stopped = _graceful_stop("switch")
        if not stopped:
            set_active(cur)
            raise ProfileBusy("the runner did not stop in time; stop the runner first (crewboss-runner service stop from an administrator "
                              "PowerShell, or Ctrl+C in its terminal) and run this again")
        say(f"Now connected to {server}: the service will reconnect to {server} in a few seconds.")
        return {"name": name, "server": cfg.get("server", ""), "restarted": True}
    via_autostart = False
    if running:
        from . import service
        with as_profile(pin) if pin else contextlib.nullcontext():
            try:
                via_autostart = service.control(service.detect(), "stop", force=True) == 0
            except Exception:  # noqa: BLE001 — no autostart installed
                via_autostart = False
            if store.run_alive() and not _graceful_stop():
                raise ProfileBusy("the runner did not stop in time; stop the runner first (crewboss-runner service stop, or Ctrl+C in its terminal) and run this again")
    set_active(name)
    restarted = False
    if running and restart:
        if via_autostart:
            from . import service
            restarted = service.control(service.detect(), "start") == 0
        if not restarted:
            say("The runner was stopped and has not been started again here: run `crewboss-runner service start` (or `crewboss-runner run`).")
    say(f"Now connected to {server}")
    return {"name": name, "server": cfg.get("server", ""), "restarted": restarted}


def rename(old: str, new: str) -> None:
    """Move the state first, then the config, then set the active name; any OSError rolls back what was done and becomes a ProfileError."""
    check_name(new)
    if not exists(old):
        raise ProfileError(f"no profile {old!r}")
    if exists(new) or store.profile_config_dir(new).exists():
        raise ProfileError(f"a profile named {new!r} already exists")
    if _alive(old):
        raise ProfileError(f"profile {old!r} is running: stop the runner first")
    osec = store.Secrets(profile=old)
    want = {n: v for n in SECRET_NAMES if (v := osec.get(n))}
    nsec = store.Secrets(profile=new)
    src_cfg, dst_cfg = store.profile_config_dir(old), store.profile_config_dir(new)
    src_state, dst_state = store.profile_state_dir(old), store.profile_state_dir(new)
    was_active = store.read_active() == old
    done: list[str] = []
    try:
        if nsec.backend == "os-keyring":   # copy and verify under the new service name before anything is moved
            for n, v in want.items():
                nsec.set(n, v)
            done.append("keyring")
            if want and not _verify(nsec, want):
                raise ProfileError("could not verify the copied key and token; nothing was changed")
        if src_state.exists():
            dst_state.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src_state, dst_state)
            done.append("state")
        dst_cfg.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src_cfg, dst_cfg)
        done.append("config")
        if was_active:
            set_active(new)
    except (OSError, ProfileError) as exc:
        with contextlib.suppress(OSError):
            if "config" in done:
                os.replace(dst_cfg, src_cfg)
            if "state" in done:
                os.replace(dst_state, src_state)
        if "keyring" in done:
            for n in SECRET_NAMES:
                with contextlib.suppress(Exception):
                    nsec.delete(n)
        with contextlib.suppress(OSError):
            if was_active:
                set_active(old)
        raise exc if isinstance(exc, ProfileError) else ProfileError(f"could not rename the profile ({exc}); nothing was changed") from exc
    if osec.backend == "os-keyring":
        for n in SECRET_NAMES:
            osec.delete(n)


def remove(name: str) -> str:
    """Delete one profile's key, token, config and state. Returns the server it pointed at (for the revoke reminder)."""
    if not exists(name):
        raise ProfileError(f"no profile {name!r}")
    if active_profile() == name:
        others = [n for n in names() if n != name]
        raise ProfileError(f"{name!r} is the active profile; " + (f"switch first: crewboss-runner use {others[0]}" if others else
                           "it is the only one: use `crewboss-runner uninstall` to remove everything"))
    if _alive(name):
        raise ProfileError(f"profile {name!r} is running: stop it first")
    server = (store.Config(profile=name).load() or {}).get("server", "")
    sec = store.Secrets(profile=name)
    for n in SECRET_NAMES:
        sec.delete(n)
    shutil.rmtree(store.profile_config_dir(name), ignore_errors=True)
    shutil.rmtree(store.profile_state_dir(name), ignore_errors=True)
    return server


def format_table(rows: list[dict]) -> str:
    if not rows:
        return "no profiles (run: crewboss-runner enrol <code>)"
    head = ("", "PROFILE", "SERVER", "RUNNER", "STATE")
    body = [("*" if r["active"] else " ", r["name"], r["server"], r["runner_name"], r["state"]) for r in rows]
    widths = [max(len(str(x[i])) for x in [head] + body) for i in range(5)]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip() for row in [head] + body) + "\n(* = active)"
