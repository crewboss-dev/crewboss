"""Test-only helpers for the Assets tests (task #778): a project tree fixture and an IMPORTER from today's data (materials,
account shared skills and rules) into assets, so the resolver can be compared with what runs today. The real migration
import is phase P3; this one is deliberately simple and lives only in the tests."""
import re

from app import asset_store, materials as mat, repo, tree


def make_tree():
    """Acme (account) -> app -> sub, plus app2 (a second child of Acme); Beta (account) -> bapp; solo (top level, no account)."""
    def mk(slug, parent=None, kind="project"):
        p = repo.create_project(slug, slug.title(), "client", "owner", short_name=slug[:10], session_roles=[])
        if parent is not None or kind != "project":
            tree.set_place(p["id"], "owner", parent["id"] if parent else None, kind)
        return repo.get_project(p["id"])
    acme = mk("acme", kind="account")
    app = mk("app", acme)
    sub = mk("sub", app)
    app2 = mk("app2", acme)
    beta = mk("beta", kind="account")
    bapp = mk("bapp", beta)
    solo = mk("solo")
    return dict(acme=acme, app=app, sub=sub, app2=app2, beta=beta, bapp=bapp, solo=solo)


def skill_fields(desc="Use when building.", **kw):
    return {"description": desc, **kw}


def rule_fields(**kw):
    return {"kind": "rule", "severity": "must", **kw}


def mk_skill(owner, key, scope="project", share_all=False, body="# Steps\n", **kw):
    return asset_store.create("skill", key, key.title(), scope, owner["id"] if owner else None, skill_fields(**kw.pop("fields", {})),
                              body, share_all=share_all, actor="test", **kw)


def mk_rule(owner, key, scope="project", share_all=False, body="Do it.", fields=None, **kw):
    return asset_store.create("rule", key, key.title(), scope, owner["id"] if owner else None, rule_fields(**(fields or {})),
                              body, share_all=share_all, actor="test", **kw)


# --- the test-only importer ---------------------------------------------------------------------------------------------

def import_materials() -> None:
    """Every (project, slot) version chain becomes one material asset with all its versions (same files, same hashes);
    account-owned ones are share_all (today's account inheritance), and a non-account project's materials are shared with
    each descendant for which it is the NEAREST ancestor that has the slot (today's nearest-wins, made explicit)."""
    idx = tree.index()
    assets: dict[tuple[int, str], dict] = {}
    for pid, p in sorted(idx.items()):
        rows = mat.own(pid, include_old=True)
        by_slot: dict[str, list[dict]] = {}
        for r in rows:
            by_slot.setdefault(r["slot"], []).append(r)
        for slot, chain in by_slot.items():
            chain.sort(key=lambda r: r["version"])
            scope = "account" if tree.is_account(p) else "project"
            a = None
            for r in chain:
                fields = {"kind": r["kind"], "title": r["title"], "purpose": r["purpose"], "usage_notes": r["usage_notes"]}
                files = [{"path": r["filename"], "raw": mat.read_bytes(r), "rel_path": r["rel_path"]}]
                if a is None:
                    a = asset_store.create("material", slot, r["title"], scope, pid, fields, "", files, actor=r["created_by"],
                                           via="import", share_all=(scope == "account"))
                else:
                    a = asset_store.new_version(a["id"], fields, "", files, actor=r["created_by"], via="import")
            assets[(pid, slot)] = a
    for pid, d in idx.items():
        seen: set[str] = set()
        for node in tree.inherit_chain(d, idx)[1:]:
            if tree.is_account(node):
                continue
            for (npid, slot), a in assets.items():
                if npid == node["id"] and slot not in seen:
                    asset_store.share(a["id"], pid)
                    seen.add(slot)


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "item"


def import_shared_knowledge() -> None:
    """Account knowledge rows kind skill / rule -> account assets shared with every project, archived ones archived."""
    from app.db import get_cursor
    with get_cursor() as cur:
        cur.execute("SELECT * FROM knowledge WHERE kind IN ('skill', 'rule') ORDER BY created_at, id")
        rows = cur.fetchall()
    used: set[tuple] = set()
    for r in rows:
        key, i = _slug(r["title"]), 1
        while (r["project_id"], r["kind"], key) in used:
            i += 1
            key = f"{_slug(r['title'])}-{i}"
        used.add((r["project_id"], r["kind"], key))
        if r["kind"] == "skill":
            fields = {"description": r["summary"] or r["title"]}
        else:
            fields = {"kind": "instruction", "severity": "must", "locked": False}
        a = asset_store.create(r["kind"], key, r["title"], "account", r["project_id"], fields, r["body"], summary=r["summary"],
                               share_all=True, actor=r["created_by"], via="import", origin="import")
        if r["archived_at"] is not None:
            asset_store.archive(a["id"], "import")
