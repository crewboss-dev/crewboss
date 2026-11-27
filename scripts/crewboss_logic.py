#!/usr/bin/env python3
"""Run the logic map commands from anywhere: `python -I scripts/crewboss_logic.py check|uncovered|report|export|import-catalog [--dir .]`.

The two modules are loaded BY PATH under their own names, so an app that has its own `app` package (or a `yaml`-shadowing file in the
current folder) cannot take the place of Crewboss's. Needs only PyYAML. `crewboss logic ...` (the launcher) calls this."""
import importlib.util
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, APP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    _load("crewboss_logic_map", "logic_map.py")
    cli = _load("crewboss_logic_cli", "logic_cli.py")
    sys.exit(cli.main(sys.argv[1:]))
