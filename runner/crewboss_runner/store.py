"""Where the runner keeps things.

Secrets (the device ed25519 key, the device token) go in the OS keyring. Only when there is no usable keyring backend (a
headless Linux box without a secret service) do they go in a 0600 file under the config dir, and `status` says so.
Non-secret settings (server, pinned fp8 and public key, device id, mode) are a JSON file in the config dir.
Job material (worktrees, the per-job MCP config that holds the job token) lives under the state dir, never inside a worktree."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from . import settings

SERVICE = "crewboss-runner"
LEGACY_SERVICE = "mallen-runner"   # TODO(#589 compat): a runner enrolled before the rename keeps its secrets here


def _legacy_aware(root: Path) -> Path:
    """<root>/crewboss-runner; but a runner enrolled before the rename keeps using <root>/mallen-runner while that exists and the
    new folder does not (TODO(#589 compat)): renaming it would silently un-enrol the PC."""
    new, old = root / "crewboss-runner", root / "mallen-runner"
    return old if old.is_dir() and not new.exists() else new


def root_config_dir() -> Path:
    """The folder that holds every profile's config (profiles/<name>/) and the `active` file. A runner enrolled before profiles keeps
    its config.json here until the first use migrates it (see profiles.migrate_legacy)."""
    base = settings.get("CONFIG_DIR")
    if base:
        return Path(base)
    if os.name == "nt":   # Windows: the user profile's AppData, not ~/.config
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "crewboss-runner"
    return _legacy_aware(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"))


def root_state_dir() -> Path:
    base = settings.get("STATE_DIR")
    if base:
        return Path(base)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "crewboss-runner" / "state"
    return _legacy_aware(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"))


# ---- profiles (task #975): one PC, several Crewboss servers, one active at a time ----
import re   # noqa: E402

PROFILES = "profiles"
ACTIVE_FILE = "active"
DEFAULT_PROFILE = "default"
NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")   # safe in a path, a keyring service name and a task name
_override: str | None = None   # `--profile X` for this process; the CREWBOSS_RUNNER_PROFILE environment variable does the same
_migration_failed: set[str] = set()


def valid_name(name: str) -> bool:
    return bool(isinstance(name, str) and NAME_RE.fullmatch(name)) and not name.startswith("-")   # fullmatch: "abc\n" is refused


def set_profile_override(name: str | None) -> None:
    global _override
    if name is not None and not valid_name(name):
        raise ValueError(f"bad profile name {name!r}: use 1-32 of a-z, 0-9 and -")
    _override = name


def read_active() -> str | None:
    try:
        name = (root_config_dir() / ACTIVE_FILE).read_text().strip()
    except OSError:
        return None
    return name if valid_name(name) else None


def legacy_layout_present() -> bool:
    return (root_config_dir() / "config.json").is_file()


_no_migrate = False   # set by the Windows service's supervisor (an admin process): it never migrates, and never reads the user-writable `active` file


def forbid_migration() -> None:
    global _no_migrate
    _no_migrate = True


def _legacy_mode() -> bool:
    """True while the single-enrolment layout is the one in use: a legacy config.json exists and could not (or must not yet) be
    migrated. The migration is deferred while the legacy runner is alive (its run.pid in the ROOT state folder), so that its status,
    job folders and pid are never stranded; run_alive / job_running / the stop file then all look at the root state."""
    if not legacy_layout_present():
        return False
    if _no_migrate:
        return True
    root = str(root_config_dir())
    if root in _migration_failed or run_alive(root_state_dir() / "run.pid"):
        return True
    from . import profiles   # lazy: profiles imports this module
    try:
        profiles.migrate_legacy()
    except profiles.MigrationBusy:
        return True
    except Exception:  # noqa: BLE001 — the old layout keeps working, and is retried next run
        _migration_failed.add(root)
        return True
    return False


def ensure_migrated() -> None:
    """Run the lazy migration now (before any --profile override is applied)."""
    if not _no_migrate:
        _legacy_mode()


def current_profile() -> str | None:
    """The profile every command acts on: --profile, else CREWBOSS_RUNNER_PROFILE, else the `active` file, else "default".
    The migration of a legacy enrolment runs FIRST, so an override never skips it. None means the legacy single-enrolment layout
    (migration deferred or failed): it still works."""
    name = _override or settings.get("PROFILE")
    if name and not valid_name(name):
        raise ValueError(f"bad profile name {name!r}: use 1-32 of a-z, 0-9 and -")
    legacy = _legacy_mode()
    if name:
        return name
    if _no_migrate:
        return None
    return None if legacy else (read_active() or DEFAULT_PROFILE)


def profile_config_dir(name: str) -> Path:
    return root_config_dir() / PROFILES / name


def profile_state_dir(name: str) -> Path:
    return root_state_dir() / PROFILES / name


def config_dir() -> Path:
    """The ACTIVE profile's config folder (so service, doctor, stopctl, runlog... all act on the active profile with no change)."""
    name = current_profile()
    return root_config_dir() if name is None else profile_config_dir(name)


def state_dir() -> Path:
    name = current_profile()
    return root_state_dir() if name is None else profile_state_dir(name)


def keyring_service(profile: str | None) -> str:
    return SERVICE if profile is None else f"{SERVICE}/{profile}"


OWN_NAMES = ("crewboss-runner", "mallen-runner")
MARKER = ".crewboss-runner-dir"   # written into the config and state folders on first use: uninstall deletes only folders that carry it (or are named like ours)


def mark_dir(path: Path) -> Path:
    if path.parent.name == PROFILES:   # profiles/<name>: the root above it is ours too (uninstall --all deletes it)
        mark_dir(path.parent.parent)
    ensure_private_dir(path)
    marker = path / MARKER
    if not marker.exists():
        marker.write_text("crewboss-runner state; safe for `crewboss-runner uninstall` to remove\n")
    return path


def pid_file() -> Path:
    return state_dir() / "run.pid"


def read_pid() -> int | None:
    """The pid in run.pid as a plain positive integer, else None. The file is user-writable: nothing else is ever taken from it."""
    try:
        text = pid_file().read_text(errors="replace").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() and len(text) <= 10 and int(text) > 0 else None


def write_pid() -> None:
    """run.pid in this profile's state folder, and the fixed ROOT live.pid (what the Windows service's supervisor reads)."""
    write_private(pid_file(), str(os.getpid()))
    write_private(live_pid_file(), str(os.getpid()))


def release_pid() -> None:
    """Remove run.pid (and live.pid), but only if it still holds THIS process's pid (a newer runner may already have written its own)."""
    if read_pid() == os.getpid():
        try:
            pid_file().unlink()
        except OSError:
            pass
    try:
        if int(live_pid_file().read_text().strip()) == os.getpid():
            live_pid_file().unlink()
    except (OSError, ValueError):
        pass


def live_pid_file() -> Path:
    """A fixed ROOT-level file naming the running runner's pid, whichever profile it runs: the Windows service's supervisor (an admin
    process that must not read the user-writable `active` file) finds the runner to stop from here."""
    return root_state_dir() / "live.pid"


def run_alive(pid_path: Path | None = None) -> bool:
    """True when a `crewboss-runner run` on this PC holds the pid file (this profile's, or the given one) and that process exists."""
    try:
        pid = int((pid_path or pid_file()).read_text().strip())
    except (OSError, ValueError):
        return False
    return pid == os.getpid() or pid_alive(pid)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":   # Windows: the profile folder's ACL already limits it to the user; chmod does not apply
        os.chmod(path, 0o700)
    return path


def write_private(path: Path, data: str) -> None:
    """Create/replace a file as 0600 from the first byte (no window where it is world-readable)."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.replace(tmp, path)


def _keyring():
    """The keyring module if it has a real backend, else None."""
    if settings.get("NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001 — not installed, or the backend is broken
        return None


class Secrets:
    """get/set/delete of named secrets; the backend is chosen once."""

    def __init__(self, cfg: Path | None = None, profile: str | None = ...):
        if profile is ...:
            profile = current_profile() if cfg is None else (cfg.name if cfg.parent.name == PROFILES else current_profile())
        self.profile = profile
        self.service = keyring_service(profile)
        self.cfg = cfg or (root_config_dir() if profile is None else profile_config_dir(profile))
        self._kr = _keyring()
        self.backend = "os-keyring" if self._kr else "file"
        self.file = self.cfg / "secrets.json"

    def describe(self) -> str:
        return ("OS keyring" if self._kr else f"0600 file {self.file} (no OS keyring backend on this machine)")

    def _file(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, name: str) -> str | None:
        if self._kr:
            # TODO(#589 compat): a secret saved before the rename sits under the old keyring service
            got = self._kr.get_password(self.service, name)
            return got or (self._kr.get_password(LEGACY_SERVICE, name) if self.profile is None else None)
        return self._file().get(name)

    def set(self, name: str, value: str) -> None:
        if self._kr:
            self._kr.set_password(self.service, name, value)
            return
        data = self._file()
        data[name] = value
        write_private(self.file, json.dumps(data))

    def delete(self, name: str) -> None:
        if self._kr:
            for service in ((self.service,) if self.profile else (SERVICE, LEGACY_SERVICE)):
                try:
                    self._kr.delete_password(service, name)
                except Exception:  # noqa: BLE001 — already gone
                    pass
            return
        data = self._file()
        if data.pop(name, None) is not None:
            write_private(self.file, json.dumps(data))

    def file_mode_ok(self) -> bool:
        if self._kr or not self.file.exists():
            return True
        return os.name == "nt" or stat.S_IMODE(self.file.stat().st_mode) == 0o600


class Config:
    """Non-secret settings: server (host:port), scheme, fp8, server_public_key, device_id, mode, name, expires_at."""

    def __init__(self, cfg: Path | None = None, profile: str | None = ...):
        if cfg is None and profile is not ...:
            cfg = root_config_dir() if profile is None else profile_config_dir(profile)
        self.path = (cfg or config_dir()) / "config.json"

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None

    def save(self, data: dict) -> None:
        write_private(self.path, json.dumps(data, indent=2))
        mark_dir(self.path.parent)

    def delete(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass
