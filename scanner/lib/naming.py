"""Group/id/name conventions shared by build.py — the fixed vocabulary from docs/catalog-schema.md
and the owner's naming rule (`Group › Thing › Action`, id `group.thing.action`)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from scanner.lib.astutil import slugify

GROUPS = [
    "Sales", "Service", "Monitoring", "Billing", "Email and AiOS", "Remote access",
    "Documents and Sign", "Platform",
]
GROUP_SLUG = {
    "Sales": "sales", "Service": "service", "Monitoring": "monitoring", "Billing": "billing",
    "Email and AiOS": "email", "Remote access": "remote-access", "Documents and Sign": "documents",
    "Platform": "platform",
}

# Best-effort default group for an auto-discovered worker/webhook/decision/route file by its file-derived name,
# used ONLY when nothing in the annotations claims that source file. Deliberately coarse; accuracy for anything
# that matters lives in the annotation. The tables are INSTANCE configuration: empty here (everything lands in
# "Platform"), filled from $CREWBOSS_HOME/scanner/naming.yaml by load_overrides(). See scanner/naming.example.yaml.
WORKER_GROUP: dict[str, str] = {}          # worker file stem -> group
WEBHOOK_GROUP: dict[str, str] = {}         # webhook path -> group
DECISION_GROUP: dict[str, str] = {}        # decision type -> group
ROUTE_FILE_GROUP: dict[str, str] = {}      # route file stem -> group, for the auto "<Entity> > Actions" workflows
ROUTE_ENTITY_GROUP: dict[str, str] = {}    # entity-level override where one route file spans two domains

_TABLES = {
    "worker_group": WORKER_GROUP, "webhook_group": WEBHOOK_GROUP, "decision_group": DECISION_GROUP,
    "route_file_group": ROUTE_FILE_GROUP, "route_entity_group": ROUTE_ENTITY_GROUP,
}


def naming_path() -> str:
    return os.environ.get("CREWBOSS_NAMING") or str(Path(os.environ.get("CREWBOSS_HOME") or Path.home() / ".crewboss") / "scanner" / "naming.yaml")


def load_overrides(path: str | Path | None = None) -> None:
    """Reset the tables to the (empty) defaults, then fill them from the instance's naming.yaml. Absent file = no
    overrides. A table that is not a mapping of strings, or an unknown key, is an error (a typo must not silently
    drop a grouping)."""
    for t in _TABLES.values():
        t.clear()
    p = Path(path or naming_path())
    if not p.exists():
        return
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: must be a mapping of table name -> {{name: group}}")
    for key, table in data.items():
        if key not in _TABLES:
            raise ValueError(f"{p}: unknown table '{key}' (expected one of {', '.join(_TABLES)})")
        if table is None:
            continue
        if not isinstance(table, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in table.items()):
            raise ValueError(f"{p}: '{key}' must map names to group names (strings)")
        _TABLES[key].update(table)


def make_id(group: str, thing: str, action: str) -> str:
    return f"{GROUP_SLUG.get(group, slugify(group))}.{slugify(thing)}.{slugify(action)}"


def make_name(group: str, thing: str, action: str) -> str:
    return f"{group} › {thing} › {action}"
