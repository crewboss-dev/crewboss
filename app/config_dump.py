"""`python -m app.config dump`: the effective configuration as sorted JSON, secrets redacted.

Kept apart from app/config.py so it reads a config namespace without importing one (the before/after comparison in
tests/test_instance.py feeds it a namespace of its own). Only UPPER_CASE public names are config; a name that holds a
secret VALUE (not a path to a secret file) is redacted, and so is the password inside a URL."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

REDACTED = "***"
# Names whose value IS a secret. *_FILE names hold a path, which is not one.
_SECRET_NAME = re.compile(r"(SECRET|PASSWORD|TOKEN|API_KEY|PRIVATE_KEY)$")


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.password is None:
        return value
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=f"{parts.username or ''}:{REDACTED}@{host}"))


def _plain(value):
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def effective(ns: dict) -> dict:
    out = {}
    for name in sorted(ns):
        if not name.isupper() or name.startswith("_"):
            continue
        value = ns[name]
        if callable(value):
            continue
        if _SECRET_NAME.search(name) and value:
            out[name] = REDACTED
        elif isinstance(value, str):
            out[name] = _redact_url(value)
        else:
            out[name] = _plain(value)
    return out


def dump(ns: dict) -> str:
    return json.dumps(effective(ns), indent=2, sort_keys=True)
