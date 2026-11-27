"""Instance configuration: the values that belong to ONE install of Crewboss, not to the code.

Each install keeps its own data and config OUTSIDE the repo, in CREWBOSS_HOME (env var; default ~/.crewboss; inside the
container it is mounted at /crewboss-home). The folder holds:

    instance.yaml       public URLs and hosts, server names, platform key stores, project ROLE slugs, add-ons
    drift.yaml          linked admin sites for the drift check (app/drift.py)
    health_feeds.yaml   how each project's health reaches the app (app/health_reports.py)
    seed.yaml           optional first-run seed project and tasks (app/seed.py)
    denylist.txt        optional: values the commit guard keeps out of the repo (scripts/hooks/pre-commit)

instance.yaml only replaces the hard-coded FALLBACKS in app/config.py: an environment variable still wins. A missing or
empty file means a fresh install: neutral defaults, the app still starts. A file that is not a mapping is a loud error
at start-up, never a silent fall-back. Read once at import.
"""
from __future__ import annotations

import os

import yaml

HOME = os.environ.get("CREWBOSS_HOME") or os.path.join(os.path.expanduser("~"), ".crewboss")
INSTANCE_FILE = os.path.join(HOME, "instance.yaml")


def path(name: str) -> str:
    """A file in the instance folder (CREWBOSS_HOME)."""
    return os.path.join(HOME, name)


def load(path: str | None = None) -> dict:
    """The parsed instance.yaml. A missing or empty file is {} (a fresh install); one that is not a mapping raises."""
    path = path or INSTANCE_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: instance.yaml must be a mapping at the top level")
    return data


DATA = load()


def _bad(key: str, want: str, value) -> ValueError:
    return ValueError(f"{INSTANCE_FILE}: {key!r} must be {want}, not {type(value).__name__} ({value!r})")


def get(key: str, default=None, data: dict | None = None):
    """A value by dotted key ("roles.overseer"); `default` when any step is missing or the value is null."""
    cur = DATA if data is None else data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur


def get_str(key: str, default: str = "") -> str:
    """A string value. Null/absent = unset (the default); a number, bool, list or mapping raises, naming file and key."""
    value = get(key, None)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _bad(key, "a string", value)
    return value


def get_bool(key: str, default: bool = False) -> bool:
    """A yes/no value: a YAML bool, or the strings yes/no, true/false, on/off, 1/0. Null/absent = the default; anything else raises."""
    value = get(key, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on", "0", "false", "no", "off", ""):
        return value.strip().lower() in ("1", "true", "yes", "on")
    raise _bad(key, "yes or no", value)


def get_list(key: str, default: tuple | list = ()) -> list:
    """A list of strings (a bare string is one item); the default when null/absent. Anything else raises."""
    value = get(key, None)
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise _bad(key, "a list of strings", value)
    for item in value:
        if not isinstance(item, str):
            raise _bad(key, "a list of strings", item)
    return list(value)


def get_map(key: str, default: dict | None = None) -> dict:
    value = get(key, None)
    if value is None:
        return dict(default or {})
    if not isinstance(value, dict):
        raise _bad(key, "a mapping", value)
    return value
