"""Scanner settings for THIS install. They live in the instance folder, never in the repo.

    $CREWBOSS_HOME/scanner/annotations.yaml   the hand-curated workflows (scanner/annotations.py); absent = none, all AUTO
    $CREWBOSS_HOME/scanner/health_map.yaml    optional: (source, category) -> (workflow, step) for a health page
    $CREWBOSS_HOME/scanner/scanner.yaml       which repos to scan and where the rescan job imports (keys below)

CREWBOSS_HOME defaults to ~/.crewboss, the same as app/instance.py. scanner.example.yaml in this folder shows every key.

scanner.yaml keys (all optional):
    project:      the dashboard project the catalog belongs to (default: instance.yaml `catalog_project`)
    ref:          git ref to scan (default origin/main)
    repos:        {primary, secondary, knowledge}: checkout paths. `primary` is the application; `secondary` is a companion
                  app (annotations with subproject LinkedIn); `knowledge` is the host-automation repo (subproject
                  Infrastructure). A repo with no path is not scanned. The catalog's `commits` map is keyed by each path's
                  folder name.
    import_url:   the dashboard's /api/catalog/import (default http://127.0.0.1:8931/api/catalog/import)
    token_file:   the scanner-role token the rescan job reads (default ~/.config/crewboss/tokens/scanner)
    annotations:  `optional` = this install has no annotations file (all workflows AUTO). Otherwise a configured scan REQUIRES the
                  file: the rescan fails that tick rather than importing an empty catalog over the real one.
    state_dir:    the rescan job's fingerprint and status files (default ~/.local/state/crewboss)

Environment overrides: CREWBOSS_ANNOTATIONS, CREWBOSS_SCANNER_CONFIG, CREWBOSS_DASH_IMPORT_URL.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

DEFAULT_REF = "origin/main"
DEFAULT_IMPORT_URL = "http://127.0.0.1:8931/api/catalog/import"
ROLES = ("primary", "secondary", "knowledge")


def home() -> Path:
    return Path(os.environ.get("CREWBOSS_HOME") or Path.home() / ".crewboss")


def scanner_dir() -> Path:
    return home() / "scanner"


def annotations_path() -> str:
    return os.environ.get("CREWBOSS_ANNOTATIONS") or str(scanner_dir() / "annotations.yaml")


def health_map_path() -> str:
    return str(scanner_dir() / "health_map.yaml")


def _expand(value) -> str:
    return os.path.expanduser(str(value)) if value else ""


def load() -> dict:
    """The parsed scanner.yaml; {} when absent. A file that is not a mapping is an error, never a silent fall-back."""
    path = Path(os.environ.get("CREWBOSS_SCANNER_CONFIG") or scanner_dir() / "scanner.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: scanner.yaml must be a mapping at the top level")
    return data


def project() -> str:
    from app import instance
    return str(load().get("project") or instance.get_str("catalog_project") or "")


def ref() -> str:
    return str(load().get("ref") or DEFAULT_REF)


def repo_path(role: str) -> str:
    repos = load().get("repos") or {}
    return _expand(repos.get(role))


def repo_label(path: str, fallback: str) -> str:
    return Path(path).name if path else fallback


def labels() -> dict[str, str]:
    """{role: label} for the catalog's `commits` map: each configured repo's folder name."""
    return {r: repo_label(repo_path(r), r) for r in ROLES}


def annotations_required() -> bool:
    """A scan that names repos needs its annotations file unless scanner.yaml says `annotations: optional`."""
    cfg = load()
    return bool(cfg.get("repos")) and str(cfg.get("annotations", "")).lower() != "optional"


def import_url() -> str:
    return os.environ.get("CREWBOSS_DASH_IMPORT_URL") or str(load().get("import_url") or DEFAULT_IMPORT_URL)


def token_file() -> str:
    cfg = _expand(load().get("token_file"))
    if cfg:
        return cfg
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return str(Path(base) / "crewboss" / "tokens" / "scanner")


def state_dir() -> str:
    cfg = _expand(load().get("state_dir"))
    if cfg:
        return cfg
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return str(Path(base) / "crewboss")
