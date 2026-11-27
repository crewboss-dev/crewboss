"""Project ROLES as data: which project (by slug) plays each part in this install.

The parts are fixed by the code; WHO plays them is the instance's (instance.yaml `roles:`, an CREWBOSS_* env var over it —
both land in app/config.py). Everything that used to compare a session's or project's slug with a literal goes through
here, so no install's project names live in the logic:

    overseer       the project whose 'all' session reads across accounts (dash); the root of the tree
    health         the project that owns System Health and the business pulse; its account is the estate account
    default        where a no-token read that names no project lands
    legacy_token   whose project a session token with no project_id belongs to
    export         whose catalog is written out for the ERP
    dashboard      the Dashboard's own project

A role nobody fills (empty slug) matches NOTHING, and an unknown role NAME is a programming error that raises. That is
not the same as "a missing role is safe": the OVERSEER role must be filled. With no overseer the tree has no global
root, and tree.account_root_id then folds every top-level, non-account project into one partition — access widens.
a fresh install (no instance.yaml in CREWBOSS_HOME) starts with every role unfilled, so main.py logs that loudly, and
warn_unmatched() logs a role whose slug is not a current project. Matching is exact: no case folding, no trimming, and
an old slug (alias) of the role's project is not the role. The slug is read from config at call time, so a test (or a
restart with new settings) moves a role with it."""
from __future__ import annotations

import logging

from app import config

log = logging.getLogger("crewboss.roles")

# role -> the config setting that carries its slug.
ROLES = {
    "overseer": "OVERSEER_PROJECT",
    "health": "HEALTH_PROJECT",
    "default": "DEFAULT_PROJECT",
    "legacy_token": "LEGACY_TOKEN_PROJECT",
    "export": "EXPORT_PROJECT",
    "dashboard": "DASHBOARD_PROJECT",
}


def _attr(role: str) -> str:
    try:
        return ROLES[role]
    except KeyError:
        raise ValueError(f"unknown project role {role!r} (known: {', '.join(ROLES)})") from None


def slug(role: str) -> str:
    """The slug that plays `role` here; '' when the role is unfilled. An unknown role name raises ValueError."""
    return getattr(config, _attr(role), "") or ""


def is_role(project, role: str) -> bool:
    """Does this project (a row with a "slug", or a bare slug) play `role`? False for None and for an unfilled role;
    an unknown role name raises ValueError."""
    want = slug(role)                                     # raises on an unknown role, whatever the project is
    if not want or not project:
        return False
    return (project if isinstance(project, str) else project.get("slug")) == want


def holder(projects, role: str):
    """The first project of `projects` (rows, or an {id: row} index) that plays `role`; None when none does."""
    _attr(role)                                           # an unknown role raises even with nothing to search
    rows = projects.values() if isinstance(projects, dict) else projects
    return next((p for p in rows if is_role(p, role)), None)


def assigned() -> dict[str, str]:
    """{role: slug} for the roles this install has filled."""
    return {role: s for role in ROLES if (s := slug(role))}


def unmatched(current_slugs) -> dict[str, str]:
    """{role: slug} for each filled role whose slug is not one of `current_slugs` (a project's CURRENT slugs): the
    role then matches no project — e.g. the project was renamed and the role still names its old slug (an alias)."""
    current = set(current_slugs)
    return {role: s for role, s in assigned().items() if s not in current}


def warn_unmatched() -> dict[str, str]:
    """Start-up check: log a WARNING for every filled role that names no current project. Log only."""
    from app import repo
    bad = unmatched(p["slug"] for p in repo.list_projects(include_archived=True))
    for role, s in bad.items():
        log.warning("project role %r is set to %r, which is not a current project slug (renamed? not created yet?); "
                    "the role matches nothing until it is fixed in instance.yaml", role, s)
    return bad
