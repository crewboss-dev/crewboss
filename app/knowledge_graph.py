"""How a project's knowledge entries link together (task #550): backlinks under each entry, the Knowledge page's
graph view, and its housekeeping lens. READ-ONLY: nothing here changes an entry; links are read out of the text.

What counts as a link, in an entry's title, summary, body or archive reason (code spans and fenced code skipped):
  [[slug-or-title]]   a WIKI link to another entry, matched on its title, case and punctuation ignored, so
                      [[Xero tokens expire]] and [[xero-tokens-expire]] both reach "Xero tokens expire".
                      [[target|label]] and [[target#heading]] link to `target`. No entry by that name = a BROKEN link.
  supersedes [[X]] / replaces / reverses           this entry SUPERSEDES X (X is greyed in the graph);
  superseded by [[X]] / replaced by / reversed by  X supersedes THIS entry. The knowledge table has no
                      supersedes column, so the words right before a wiki link are the relation.
  #548                a TASK reference: an edge to task #548, if that task is in this project's account partition
                      (another account's task is dropped, exactly like a missing one, so a number can't probe it).
And from the other side: an OPEN task referencing an entry (its links.knowledge, or a [[wiki]] link in its title or
body) is a task edge into that entry. Done tasks' links are history, not use, and are left out.

A link resolves within the project's own entries (every kind, archived ones too) and then what it inherits from its
account (shared.inherited); own entries win, then live before archived, then the most recently updated.

build(project) is cheap but not free (every body is read), so the result is cached per project behind a fingerprint
of the rows it was built from: any edit, archive, restore or task change makes a new fingerprint and a rebuild."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from app import access, shared, tree
from app.db import get_cursor

STALE_DAYS = 90
# Kinds the Knowledge page lists: they are always graph nodes. Any other entry (an objective, an account's rule or
# skill, an inherited entry) becomes a node only when something links it.
PAGE_EXCLUDED_KINDS = ("objective", "rule", "skill")

_FENCED_RE = re.compile(r"^[ \t]*(```|~~~)[^\n]*\n.*?^[ \t]*\1[ \t]*$", re.S | re.M)
_CODE_SPAN_RE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.S)
# [[target]], [[target|label]], [[target#heading]]: one line, no nested brackets.
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\n|#]+)(?:#[^\[\]\n|]*)?(?:\|[^\[\]\n]*)?\]\]")
# #548 — not part of a word, an HTML entity (&#38;), a URL fragment (/page#12) or '##'.
TASK_REF_RE = re.compile(r"(?<![\w&#/])#([1-9][0-9]{0,8})(?![\w])")
_SUPERSEDED_BY_RE = re.compile(r"\b(?:superseded|replaced|reversed)\s+by[\s:]*$", re.I)
_SUPERSEDES_RE = re.compile(r"\b(?:supersedes|replaces|reverses)[\s:]*$", re.I)


def link_key(text: str | None) -> str:
    """The name a wiki link matches on: lower case, every run of non-alphanumerics one '-', trimmed.
    'Xero tokens: expire!' -> 'xero-tokens-expire'."""
    return re.sub(r"[^0-9a-z]+", "-", (text or "").lower()).strip("-")


def _strip_code(text: str) -> str:
    return _CODE_SPAN_RE.sub(" ", _FENCED_RE.sub("\n", text or ""))


def extract_links(text: str | None) -> dict:
    """The references in one piece of Markdown: {"wiki": [(target text, rel)], "tasks": [task numbers]}, each in
    order of first appearance, duplicates dropped. rel is 'link', 'supersedes' (this text supersedes the target) or
    'superseded_by' (the target supersedes this text's entry)."""
    clean = _strip_code(text)
    wiki: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in WIKI_LINK_RE.finditer(clean):
        target = " ".join(m.group(1).split())
        key = link_key(target)
        if not key or key in seen:
            continue
        seen.add(key)
        before = clean[max(0, m.start() - 40):m.start()]
        rel = ("superseded_by" if _SUPERSEDED_BY_RE.search(before)
               else "supersedes" if _SUPERSEDES_RE.search(before) else "link")
        wiki.append((target, rel))
    tasks: list[int] = []
    for m in TASK_REF_RE.finditer(clean):
        n = int(m.group(1))
        if n not in tasks:
            tasks.append(n)
    return {"wiki": wiki, "tasks": tasks}


def _entry_text(e: dict) -> str:
    return "\n\n".join(filter(None, (e.get("title"), e.get("summary"), e.get("body"), e.get("archive_reason"))))


def _entry_url(slug: str, e: dict) -> str:
    page = "/objectives" if e["kind"] == "objective" else shared.page_for(e["kind"])
    archived = "archived=1&" if e.get("archived_at") is not None else ""
    return f"/{slug}{page}?{archived}open={e['id']}#k-{e['id']}"


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --- the rows a graph is built from ------------------------------------------------------------------------------

def _scope(project: dict) -> tuple[list[int], list[int] | None, str]:
    """(the projects whose entries links resolve to, own first; the projects whose tasks may be referenced, or None
    for no limit — the global Dashboard level sees every account, like knowledge_titles; a signature of the project
    tree, since slugs end up in URLs and the tree decides both lists)."""
    idx = tree.index()
    accounts = [a["id"] for a in tree.account_ancestors(idx.get(project["id"], project), idx)]
    task_projects = None if tree.is_global_root(idx.get(project["id"])) else \
        sorted(tree.account_member_ids(project["id"], idx) or {project["id"]})
    tree_sig = repr(sorted((p["id"], p["slug"], p.get("parent_id"), p.get("kind"), p.get("archived_at") is None)
                           for p in idx.values()))
    # Multi-user (#713): a person's graph names only what they may see (and the cache is told whose reach built it).
    reach = access.ambient_ids()
    if reach is not None:
        allowed = set(reach)
        accounts = [a for a in accounts if a in allowed]
        task_projects = sorted(allowed if task_projects is None else set(task_projects) & allowed)
        tree_sig += "|reach:" + repr(sorted(allowed))
    return [project["id"], *accounts], task_projects, tree_sig


def _fingerprint(cur, knowledge_projects: list[int], task_projects: list[int] | None, tree_sig: str) -> str:
    cur.execute("SELECT md5(coalesce(string_agg(id || ':' || updated_at || ':' || coalesce(archived_at::text, ''), "
                "',' ORDER BY id), '')) AS f FROM knowledge WHERE project_id = ANY(%s)", (knowledge_projects,))
    k = cur.fetchone()["f"]
    if task_projects is None:
        cur.execute("SELECT md5(coalesce(string_agg(id || ':' || updated_at, ',' ORDER BY id), '')) AS f FROM tasks")
    else:
        cur.execute("SELECT md5(coalesce(string_agg(id || ':' || updated_at, ',' ORDER BY id), '')) AS f FROM tasks "
                    "WHERE project_id = ANY(%s)", (task_projects,))
    t = cur.fetchone()["f"]
    # The day is in it too: an entry turns stale with no row changing.
    return hashlib.sha256(f"{k}|{t}|{tree_sig}|{datetime.now(timezone.utc).date()}".encode()).hexdigest()


_cache: dict[int, tuple[str, dict]] = {}


def build(project: dict) -> dict:
    """The project's link graph (see compute), cached until anything it was built from changes."""
    knowledge_projects, task_projects, tree_sig = _scope(project)
    with get_cursor() as cur:
        fp = _fingerprint(cur, knowledge_projects, task_projects, tree_sig)
        hit = _cache.get(project["id"])
        if hit and hit[0] == fp:
            return hit[1]
        cur.execute("SELECT k.*, p.slug AS project_slug FROM knowledge k JOIN projects p ON p.id = k.project_id "
                    "WHERE k.project_id = ANY(%s) ORDER BY k.id", (knowledge_projects,))
        rows = cur.fetchall()
        sql = ("SELECT t.id, t.title, t.body, t.status, t.links, t.project_id, p.slug AS project_slug "
               "FROM tasks t JOIN projects p ON p.id = t.project_id")
        if task_projects is None:
            cur.execute(sql)
        else:
            cur.execute(sql + " WHERE t.project_id = ANY(%s)", (task_projects,))
        tasks = cur.fetchall()
    # An account's own items are inherited only while live and not an objective (shared.py): the rest stay hidden.
    entries = [r for r in rows if r["project_id"] == project["id"]
               or (r["archived_at"] is None and r["kind"] not in shared.NOT_INHERITED)]
    graph = compute(project, entries, tasks)
    _cache[project["id"]] = (fp, graph)
    return graph


# --- the graph itself (pure: rows in, graph out) -----------------------------------------------------------------

def compute(project: dict, entries: list[dict], tasks: list[dict], now: datetime | None = None) -> dict:
    """{nodes, edges, broken, housekeeping, kinds} for one project.
      nodes  [{id: 'k12' | 't548', type: 'entry' | 'task', ref (the row id), title, url, ...}]: an entry node has
             kind, tags, archived, superseded, inherited, stale, orphan, broken (count); a task node status, done.
      edges  [{source, target, rel, by}]: rel 'link' | 'supersedes' | 'task'. A 'supersedes' edge points from the
             newer entry to the one it supersedes, whichever side the words were written on; `by` is the node whose
             text holds the reference (what backlinks follow).
      broken [{source: node id, target: the text inside [[ ]]}]
      housekeeping  {orphans, broken, stale}: node ids (broken: the entries holding a broken link), live entries only.
    `entries` carry project_slug; own entries are those with project_id == project['id']."""
    now = now or datetime.now(timezone.utc)
    own_id = project["id"]
    by_key: dict[str, dict] = {}
    # Own entries first, then live before archived, then newest: the first to claim a name keeps it.
    ranked = sorted(entries, key=lambda e: (e["project_id"] != own_id, e.get("archived_at") is not None,
                                            -(_as_utc(e.get("updated_at")) or now).timestamp(), e["id"]))
    for e in ranked:
        by_key.setdefault(link_key(e["title"]), e)
    entry_by_id = {e["id"]: e for e in entries}
    task_by_id = {t["id"]: t for t in tasks}

    edges: list[dict] = []
    edge_seen: set[tuple] = set()
    broken: list[dict] = []
    superseded: set[int] = set()
    used_entries: set[int] = set()
    used_tasks: set[int] = set()

    def add_edge(source: str, target: str, rel: str, by: str) -> None:
        if source == target or (source, target, rel) in edge_seen:
            return
        edge_seen.add((source, target, rel))
        edges.append({"source": source, "target": target, "rel": rel, "by": by})

    sources = [e for e in entries if e["project_id"] == own_id]
    for e in sources:
        found = extract_links(_entry_text(e))
        for target_text, rel in found["wiki"]:
            target = by_key.get(link_key(target_text))
            if target is None:
                broken.append({"source": f"k{e['id']}", "target": target_text})
                continue
            if target["id"] == e["id"]:
                continue
            me, other = f"k{e['id']}", f"k{target['id']}"
            used_entries.update((e["id"], target["id"]))
            if rel == "superseded_by":
                superseded.add(e["id"])
                add_edge(other, me, "supersedes", me)
            elif rel == "supersedes":
                superseded.add(target["id"])
                add_edge(me, other, "supersedes", me)
            else:
                add_edge(me, other, "link", me)
        for n in found["tasks"]:
            if n in task_by_id:
                used_tasks.add(n)
                add_edge(f"k{e['id']}", f"t{n}", "task", f"k{e['id']}")

    # Open tasks that reference an entry: links.knowledge (Ask AI requests, session links) or a [[wiki]] link.
    open_refs: dict[int, list[int]] = {}
    for t in tasks:
        if t["status"] == "done":
            continue
        ids: list[int] = []
        linked = t["links"].get("knowledge") if isinstance(t.get("links"), dict) else None
        if not isinstance(linked, list):
            linked = [] if linked is None else [linked]
        for kid in linked:  # ids only, as knowledge_titles reads them: anything else is ignored
            if (isinstance(kid, int) and not isinstance(kid, bool)) or (isinstance(kid, str) and kid.isdigit()):
                ids.append(int(kid))
        for target_text, _rel in extract_links(f"{t.get('title') or ''}\n\n{t.get('body') or ''}")["wiki"]:
            target = by_key.get(link_key(target_text))
            if target is not None:
                ids.append(target["id"])
        for kid in dict.fromkeys(ids):
            entry = entry_by_id.get(kid)
            if entry is None or entry["project_id"] != own_id:
                continue  # only this project's own entries: an inherited one belongs to its account's graph
            used_entries.add(kid)
            used_tasks.add(t["id"])
            open_refs.setdefault(kid, []).append(t["id"])
            add_edge(f"t{t['id']}", f"k{kid}", "task", f"t{t['id']}")

    linked_nodes = {e for edge in edges for e in (edge["source"], edge["target"])}
    broken_count: dict[str, int] = {}
    for b in broken:
        broken_count[b["source"]] = broken_count.get(b["source"], 0) + 1
    stale_before = now - timedelta(days=STALE_DAYS)

    nodes: list[dict] = []
    housekeeping = {"orphans": [], "broken": [], "stale": []}
    for e in sorted(entries, key=lambda r: r["id"]):
        own = e["project_id"] == own_id
        if not ((own and e["kind"] not in PAGE_EXCLUDED_KINDS) or e["id"] in used_entries):
            continue
        nid = f"k{e['id']}"
        archived = e.get("archived_at") is not None
        updated = _as_utc(e.get("updated_at"))
        stale = bool(own and not archived and e["id"] in open_refs and updated is not None and updated < stale_before)
        orphan = own and nid not in linked_nodes and not broken_count.get(nid)
        nodes.append({
            "id": nid, "type": "entry", "ref": e["id"], "title": e["title"], "kind": e["kind"],
            "tags": list(e.get("subprojects") or []), "url": _entry_url(e["project_slug"], e),
            "archived": archived, "superseded": e["id"] in superseded, "inherited": not own,
            "updated_at": updated.isoformat() if updated else None, "stale": stale, "orphan": orphan,
            "broken": broken_count.get(nid, 0), "open_tasks": open_refs.get(e["id"], []),
        })
        if own and not archived:
            if orphan:
                housekeeping["orphans"].append(nid)
            if broken_count.get(nid):
                housekeeping["broken"].append(nid)
            if stale:
                housekeeping["stale"].append(nid)
    for n in sorted(used_tasks):
        t = task_by_id[n]
        nodes.append({"id": f"t{n}", "type": "task", "ref": n, "title": t["title"], "status": t["status"],
                      "done": t["status"] == "done", "url": f"/{t['project_slug']}/tasks?open={n}"})
    kinds = sorted({n["kind"] for n in nodes if n["type"] == "entry"})
    return {"project": project["slug"], "nodes": nodes, "edges": edges, "broken": broken,
            "housekeeping": housekeeping, "kinds": kinds, "stale_days": STALE_DAYS}


# --- reading a graph ---------------------------------------------------------------------------------------------

def linked_from(graph: dict, knowledge_id: int) -> dict:
    """What points AT an entry, for the "Linked from" list under it: {"entries": [node], "tasks": [node]} — the
    entries whose text links it (a wiki link, or superseding it), and the open tasks that reference it."""
    nid = f"k{knowledge_id}"
    by_id = {n["id"]: n for n in graph["nodes"]}
    entries: dict[str, dict] = {}
    tasks: dict[str, dict] = {}
    for edge in graph["edges"]:
        if nid not in (edge["source"], edge["target"]) or edge["by"] == nid or edge["by"] not in by_id:
            continue
        node = by_id[edge["by"]]
        if node["type"] == "task":
            tasks.setdefault(node["id"], node)
        else:
            # rel as THIS entry sees it: X 'supersedes' me, X is 'superseded_by' me, or a plain 'link'.
            rel = edge["rel"] if edge["rel"] != "supersedes" else (
                "supersedes" if edge["target"] == nid else "superseded_by")
            entries.setdefault(node["id"], dict(node, rel=rel))
    return {"entries": list(entries.values()), "tasks": list(tasks.values())}


def housekeeping_rows(graph: dict) -> dict:
    """The housekeeping lens as node rows (+ each broken link's target text), for the server-rendered panel."""
    by_id = {n["id"]: n for n in graph["nodes"]}
    targets: dict[str, list[str]] = {}
    for b in graph["broken"]:
        targets.setdefault(b["source"], []).append(b["target"])
    hk = graph["housekeeping"]
    return {
        "orphans": [by_id[i] for i in hk["orphans"]],
        "broken": [dict(by_id[i], targets=targets.get(i, [])) for i in hk["broken"]],
        "stale": [by_id[i] for i in hk["stale"]],
    }
