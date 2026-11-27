"""The header's jump box (#771): type, see matches, one click (or Enter) opens the record.

It covers projects, accounts and tasks (by #number or by words in the title). Everything is scoped by app/access.py: a person's index
holds only the projects they may see, and the task query is limited to those same ids, so a hidden record is never a result."""
from __future__ import annotations

from app import access, favourites, nav, repo, tree
from app.db import get_cursor

PER_KIND = 6


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def jump(who: "access.Principal", q: str) -> list[dict]:
    """[{kind: project|account|task, label, sub, url, archived}], records first (live before archived, name-prefix first), then tasks."""
    q = (q or "").strip()[:80]
    idx = access.filtered_index(who)
    out: list[dict] = []
    rows = [r for r in tree.project_tree(include_archived=True, idx=idx) if not tree.is_global_root(r)]
    fav = favourites.visible_ids(who, idx)
    if q:
        low = q.lower()
        hit = [r for r in rows if low in f"{r['name']} {r['short_name']} {r['slug']}".lower()]
        hit.sort(key=lambda r: (bool(r["archived_at"]), r["id"] not in fav, not r["name"].lower().startswith(low), tree.is_account(r) is False))
    else:
        projects, accounts = nav.records(who, idx)           # nothing typed: the menu's own lists, as a place to start
        # favourites first (#778), then the menu's own lists
        hit = favourites.rows(who, idx) + [r for r in accounts + projects if r["id"] not in fav]
    for r in hit[:PER_KIND * 2]:
        parent = idx.get(r.get("parent_id"))
        out.append({"kind": "account" if tree.is_account(r) else "project", "label": r["name"],
                    "sub": (parent["name"] if parent and not tree.is_global_root(parent) else r["short_name"]),
                    "url": nav.landing(r), "archived": bool(r["archived_at"]), "fav": r["id"] in fav})
    if q:
        n = repo.task_number(q)
        visible = access.visible_list(who)
        sql = ("SELECT t.id, t.title, t.status, p.slug, p.name, p.archived_at IS NOT NULL AS archived FROM tasks t "
               "JOIN projects p ON p.id = t.project_id WHERE (t.title ILIKE %s OR t.id = %s)")
        params: list = [_like(q), n or 0]
        if visible is not None:
            sql += " AND t.project_id = ANY(%s)"
            params.append(visible)
        sql += " ORDER BY (t.id = %s) DESC, t.updated_at DESC LIMIT %s"
        params += [n or 0, PER_KIND]
        with get_cursor(commit=False) as cur:
            cur.execute(sql, params)
            for t in cur.fetchall():
                out.append({"kind": "task", "label": t["title"], "sub": f"#{t['id']} · {t['name']}",
                            "url": f"/{t['slug']}/board?open={t['id']}", "archived": t["archived"]})
    return out


def jump_section(who: "access.Principal", section: str, q: str, has_account: bool = False) -> list[dict]:
    """The header search inside Administration or Assets (#778): that section's own pages (the left menu's rows), plus, in Assets, the
    skills, rules and materials of the projects the person may see whose name matches. Administration is the owner's: anyone else gets nothing."""
    from app import asset_hub
    q = (q or "").strip()[:80].lower()
    out: list[dict] = []
    if section == "admin" and not who.is_owner:
        return out
    for m in nav.section_menu(section, "", who.is_owner, has_account):
        if not q or q in m["label"].lower():
            out.append({"kind": "page", "label": m["label"], "sub": "", "url": m["href"], "archived": False})
    if section == "assets" and q:
        idx = access.filtered_index(who)
        found = asset_hub.collect_all(who, None, idx)         # each project's scan is read once for the whole search
        for t in asset_hub.TYPES:
            for g in found[t["key"]]:
                for it in g["rows"]:
                    if q in it["name"].lower() and len(out) < PER_KIND * 3:
                        out.append({"kind": t["noun"], "label": it["name"], "sub": g["project"]["name"], "url": it["href"], "archived": False})
    return out
