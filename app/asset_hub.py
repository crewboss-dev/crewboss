"""The Assets section's read-only view (#778): what already exists across the projects a person can see, grouped by project.

The skills are the real skill assets (app/asset_skills.py; #718): each project or account lists the ones it owns. Rules (the project's latest
catalog scan; an account's shared rule knowledge entries) and Materials still come from what the project pages keep. Every read goes
through app/access.py: only projects in the person's filtered index."""
from __future__ import annotations

from app import access, materials as mat, repo, rules as rules_mod, tree
from app.db import get_cursor

TYPES = (
    {"key": "skills", "label": "Skills", "noun": "skill", "blurb": "The instructions agents follow when they build, at every layer."},
    {"key": "rules", "label": "Rules & AI instructions", "noun": "rule", "blurb": "The rules and plain-language instructions that steer the workflows."},
    {"key": "materials", "label": "Materials", "noun": "material", "blurb": "Logos, templates, fonts and other files a project keeps."},
)
TYPE_BY_KEY = {t["key"]: t for t in TYPES}


def visible_projects(who, idx: dict | None = None) -> list[dict]:
    """Live projects and accounts this person may see, in tree order."""
    idx = idx if idx is not None else access.filtered_index(who)
    return list(tree.project_tree(include_archived=False, idx=idx))


def scans(project_ids: list[int]) -> dict[int, dict]:
    """{project_id: {"workflows": [...], "rules": [...] | None}} from each project's LATEST catalog scan, in ONE query that selects only those two slices
    (a live scan's catalog is large: loading it whole per project, per type, per keystroke is what this avoids)."""
    if not project_ids:
        return {}
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT DISTINCT ON (project_id) project_id, catalog->'workflows' AS workflows, catalog->'rules' AS rules "
                    "FROM catalog_scans WHERE project_id = ANY(%s) ORDER BY project_id, created_at DESC", (list(project_ids),))
        return {r["project_id"]: {"workflows": r["workflows"] or [], "rules": r["rules"]} for r in cur.fetchall()}


def skills_by_project(project_ids: list[int]) -> dict[int, list[dict]]:
    """{project id: [{uid, name, description}]} of the skill assets each project (or account) owns, in one query (#718)."""
    if not project_ids:
        return {}
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT a.owner_project_id AS pid, a.uid, a.name, v.fields->>'description' AS description FROM assets a "
                    "JOIN asset_versions v ON v.id = a.current_version_id WHERE a.type = 'skill' AND a.state = 'active' "
                    "AND a.owner_project_id = ANY(%s) ORDER BY a.key", (list(project_ids),))
        out: dict[int, list[dict]] = {}
        for r in cur.fetchall():
            out.setdefault(r["pid"], []).append(r)
        return out


def global_skill_count() -> int:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM assets WHERE type = 'skill' AND owner_scope = 'instance' AND state = 'active'")
        return cur.fetchone()["n"]


def _items(project: dict, key: str, scan: dict | None = None, skills: list | None = None) -> list[dict]:
    base = f"/{project['slug']}"
    out: list[dict] = []
    if key == "skills":
        for s in skills or []:
            out.append({"name": s["name"], "detail": s["description"] or "", "href": f"{base}/build-skills/{s['uid']}"})
    elif key == "rules" and tree.is_account(project):
        for e in repo.list_knowledge(project["id"], kind="rule"):
            out.append({"name": e["title"], "detail": e.get("summary") or "Shared by this account", "href": f"{base}/rules"})
    elif key == "rules":
        for r in rules_mod.from_raw((scan or {}).get("rules"))[0]:
            if r["enabled"]:
                out.append({"name": r["name"], "detail": rules_mod.KIND_LABEL.get(r["kind"], r["kind"]), "href": f"{base}/rules"})
    elif key == "materials":
        for m in mat.own(project["id"]):
            out.append({"name": m["title"], "detail": f"{m['kind_label']} · {m['slot']}", "href": f"{base}/materials#m-{m['slot']}"})
    return out


def collect_all(who, keys: tuple[str, ...] | None = None, idx: dict | None = None) -> dict[str, list[dict]]:
    """{type key: [{project: {slug, name}, rows: [{name, detail, href}]}]} for the asset types asked for (all by default), over the projects this person may see.
    """
    keys = keys or tuple(t["key"] for t in TYPES)
    projects = visible_projects(who, idx)
    sc = scans([p["id"] for p in projects if not tree.is_account(p)]) if "rules" in keys else {}
    sk = skills_by_project([p["id"] for p in projects]) if "skills" in keys else {}
    out: dict[str, list[dict]] = {k: [] for k in keys}
    for p in projects:
        for k in keys:
            rows = _items(p, k, sc.get(p["id"]), sk.get(p["id"]))
            if rows:
                out[k].append({"project": {"slug": p["slug"], "name": p["name"]}, "rows": rows})
    return out


def collect(who, key: str, idx: dict | None = None) -> list[dict]:
    """One asset type across the visible projects, projects with something only."""
    return collect_all(who, (key,), idx)[key]


def counts(who, idx: dict | None = None) -> dict:
    out = {k: sum(len(g["rows"]) for g in groups) for k, groups in collect_all(who, None, idx).items()}
    out["skills"] += global_skill_count()                 # the global layer is everyone's
    return out


def recent_materials(who, limit: int = 30) -> list[dict]:
    """The newest current materials across the visible projects (skills and rules carry no change dates yet)."""
    visible = access.visible_list(who)
    sql = ("SELECT m.id, m.title, m.slot, m.kind, m.created_at, m.created_by, p.slug AS project_slug, p.name AS project_name "
           "FROM materials m JOIN projects p ON p.id = m.project_id WHERE m.replaced_at IS NULL AND p.archived_at IS NULL")
    params: list = []
    if visible is not None:
        sql += " AND m.project_id = ANY(%s)"
        params.append(visible)
    sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT %s"
    params.append(limit)
    with get_cursor(commit=False) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [dict(r, kind_label=mat.KIND_LABEL.get(r["kind"], r["kind"]), href=f"/{r['project_slug']}/materials#m-{r['slot']}") for r in rows]


def recent_places(who, limit: int = 5) -> list[dict]:
    """Projects with the latest material changes, newest first, one row each."""
    seen, out = set(), []
    for r in recent_materials(who, 40):
        if r["project_slug"] in seen:
            continue
        seen.add(r["project_slug"])
        out.append({"name": r["project_name"], "href": f"/{r['project_slug']}/materials", "detail": f"{r['title']} · {r['kind_label']}", "at": r["created_at"]})
        if len(out) >= limit:
            break
    return out
