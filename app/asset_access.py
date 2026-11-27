"""Who may see and change an asset (task #718, the rights half of the Assets design). The one place; routes, CA tools and jobs ask here.

The rule follows the layer an asset lives in:
- instance (global, built-in): every signed-in person reads it; only the instance owner changes it, imports one, or confirms a licence.
- account and project: whoever may see that project (access.can_view) reads it; whoever may change it (access.can_edit) edits it; a
  project Admin or Owner (access.is_admin) confirms an imported skill's licence.
An asset that a person may not see is the same NotFound a missing one gets, so its existence never leaks."""
from __future__ import annotations

from app import access
from app.repo import Forbidden, NotFound


def signed_in(p: "access.Principal") -> bool:
    return p is not None and p.role in access.ROLES


def can_view(p, asset: dict) -> bool:
    if asset["owner_scope"] == "instance":
        return signed_in(p)
    return access.can_view(p, asset["owner_project_id"])


def can_edit(p, asset: dict) -> bool:
    if asset["owner_scope"] == "instance":
        return bool(p.is_owner)
    return access.can_edit(p, asset["owner_project_id"])


def can_confirm_licence(p, asset: dict) -> bool:
    if asset["owner_scope"] == "instance":
        return bool(p.is_owner)
    return access.is_admin(p, asset["owner_project_id"])


def can_install_global(p) -> bool:
    return bool(p.is_owner)


def require_view(p, asset: dict) -> dict:
    if not can_view(p, asset):
        raise NotFound("no such skill")
    return asset


def require_edit(p, asset: dict) -> dict:
    require_view(p, asset)
    if not can_edit(p, asset):
        raise Forbidden("you can look at this skill but not change it")
    return asset


def actor(p) -> str:
    return f"user:{p.user_id}" if getattr(p, "user_id", None) else "owner"
