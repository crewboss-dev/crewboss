"""The runner's settings from the environment: CREWBOSS_RUNNER_<NAME>.

TODO(#589 compat): before the rename these were MALLEN_RUNNER_<NAME>, and a runner already enrolled on a PC may still be started
with them (a service unit, a shell profile). Both are read, the new name first. Remove the second reading, the legacy folder and
keyring lookups in store.py and the `mallen-runner` script alias in pyproject.toml once every PC was re-enrolled or updated."""
from __future__ import annotations

import json
import os
from pathlib import Path

LEGACY_PREFIX = "MALLEN_RUNNER_"
PREFS_NAME = "prefs.json"   # the choices made in the control panel (#974), in the config folder; an environment variable always wins
PREF_NAMES = ("KEEP_AWAKE_DURING_JOBS",)   # the only settings the panel may write


def prefs_path() -> Path:
    from . import store   # store imports this module: a late import
    return store.config_dir() / PREFS_NAME


def _prefs() -> dict:
    try:
        data = json.loads(prefs_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def from_env(name: str) -> str | None:
    return os.environ.get("CREWBOSS_RUNNER_" + name) or os.environ.get(LEGACY_PREFIX + name) or None


def get(name: str, default: str | None = None) -> str | None:
    found = from_env(name)
    if found:
        return found
    if name in PREF_NAMES:
        val = _prefs().get(name.lower())
        if isinstance(val, str) and val:
            return val
    return default


def set_pref(name: str, value: str) -> None:
    """Persist one panel-writable setting (never a secret). Raises ValueError for any other name."""
    if name not in PREF_NAMES:
        raise ValueError(name)
    from . import store
    data = _prefs()
    data[name.lower()] = str(value)
    store.write_private(prefs_path(), json.dumps(data, indent=2))


def normalise_mode(value) -> str:
    """An enrol `mode` from the server: lower-cased and stripped; anything but `api` is `subscription` (fail closed: the
    subscription mode strips ANTHROPIC_API_KEY from the agent's environment)."""
    return "api" if isinstance(value, str) and value.strip().lower() == "api" else "subscription"
