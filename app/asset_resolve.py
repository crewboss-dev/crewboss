"""The Assets resolver (task #778, P2): what assets reach a project, and which version of each.

resolve(project_id, types=, job=, repo_commit=) -> ResolvedSet. One query finds every candidate (the layers below); a linear
pass in Python applies precedence. Nothing existing reads it yet (P4 switches the readers).

Layers, lowest number = broadest:
  0 built-in   instance assets with origin 'builtin' (always reach everyone)
  1 instance   other instance assets: share_all, or a share to the project's account or to the project
  2 account    the project's account's own assets: share_all, or a share to the project
  3 shared     another project's assets in the same account: a share to this project, or the owner's share_all
  4 own        this project's own assets
  5 repo       this project's repo mirror (.crewboss/ files), the latest version, or the one at `repo_commit`
Per (type, key) the HIGHEST layer wins and the rest come back as `overridden`. A pin (the consuming project's act) replaces the
live version of what it holds. LOCKED rules are the exception: a locked rule always stays; a same-key rule at a higher layer is
dropped (reported in `conflicts`) unless its fields.extends names that key, when both are delivered; a locked rule is never
pinned. Two assets at layer 3 with the same key (siblings sharing the same slot) are a tie, broken by (shared_at, id)
ascending, and reported.

`job` narrows skills and rules to the job's kind (a string such as 'build', or a mapping with `kind` or `task_types`): a skill or
rule whose fields.job_kinds is non-empty and misses the kind goes to `not_for_job` (a one-line index, not delivered in full).
Locked rules are always delivered. Rights are NOT applied here: the caller filters by what its person may see (P7).
Text isn't selected; `hydrate` loads fields, body and files for the versions a caller needs.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.db import get_cursor
from app.repo import NotFound

LAYERS = {0: "built-in", 1: "instance", 2: "account", 3: "shared", 4: "project", 5: "repo"}

_SQL = """
WITH ctx AS (SELECT %(pid)s::int AS pid, (SELECT partition_id FROM projects WHERE id = %(pid)s) AS acct),
cand AS (
  SELECT a.*, CASE WHEN a.origin = 'builtin' THEN 0 ELSE 1 END AS layer, NULL::timestamptz AS shared_at FROM assets a, ctx
   WHERE a.owner_scope = 'instance' AND a.state = 'active'
     AND (a.share_all OR a.origin = 'builtin'
          OR EXISTS (SELECT 1 FROM asset_shares s WHERE s.asset_id = a.id AND s.target_project_id IN (ctx.pid, ctx.acct)))
  UNION ALL
  SELECT a.*, 2, NULL FROM assets a, ctx
   WHERE a.owner_scope = 'account' AND a.account_id = ctx.acct AND a.state = 'active'
     AND (a.share_all OR EXISTS (SELECT 1 FROM asset_shares s WHERE s.asset_id = a.id AND s.target_project_id = ctx.pid))
  UNION ALL
  SELECT a.*, 3, (SELECT min(s.shared_at) FROM asset_shares s WHERE s.asset_id = a.id AND s.target_project_id = ctx.pid)
    FROM assets a, ctx
   WHERE a.owner_scope = 'project' AND a.account_id = ctx.acct AND a.owner_project_id <> ctx.pid AND a.state = 'active'
     AND a.origin <> 'repo'
     AND (a.share_all OR EXISTS (SELECT 1 FROM asset_shares s WHERE s.asset_id = a.id AND s.target_project_id = ctx.pid))
  UNION ALL
  SELECT a.*, CASE WHEN a.origin = 'repo' THEN 5 ELSE 4 END, NULL FROM assets a, ctx
   WHERE a.owner_project_id = ctx.pid AND a.owner_scope = 'project' AND a.state = 'active')
SELECT c.id, c.uid, c.type, c.key, c.name, c.summary, c.layer, c.shared_at, c.created_at, c.owner_scope,
       c.owner_project_id, op.slug AS owner_slug, op.name AS owner_name, c.locked,
       v.id AS version_id, v.n, v.content_hash, v.fields->>'extends' AS extends, v.fields->'job_kinds' AS job_kinds,
       v.fields->>'repo_path' AS repo_path, (p.version_id IS NOT NULL AND NOT c.locked) AS pinned
  FROM cand c
  LEFT JOIN projects op ON op.id = c.owner_project_id
  LEFT JOIN asset_pins p ON p.asset_id = c.id AND p.project_id = %(pid)s
  JOIN asset_versions v ON v.id = CASE
       WHEN p.version_id IS NOT NULL AND NOT c.locked THEN p.version_id
       WHEN c.layer = 5 AND %(commit)s::text IS NOT NULL THEN COALESCE(
            (SELECT v2.id FROM asset_versions v2 WHERE v2.asset_id = c.id AND v2.source_commit = %(commit)s
              ORDER BY v2.n DESC LIMIT 1), c.current_version_id)
       ELSE c.current_version_id END
 WHERE (%(types)s::text[] IS NULL OR c.type = ANY(%(types)s))
 ORDER BY c.type, c.key, c.layer, COALESCE(c.shared_at, c.created_at), c.id
"""


@dataclass(frozen=True)
class ResolvedAsset:
    asset_id: int
    uid: str
    type: str
    key: str
    name: str
    summary: str
    layer: int
    version_id: int
    n: int
    content_hash: str
    pinned: bool
    locked: bool
    extends: str | None
    owner: dict                  # {"scope", "project_id", "slug", "name"}; project_id/slug/name None for an instance asset
    repo_path: str | None
    created_at: object           # when the asset was made: lets a reader reproduce today's oldest-first order


@dataclass(frozen=True)
class ResolvedSet:
    project_id: int
    entries: tuple[ResolvedAsset, ...]
    overridden: tuple[ResolvedAsset, ...]
    conflicts: tuple[dict, ...]
    not_for_job: tuple[ResolvedAsset, ...]
    set_hash: str                # sha256 of the sorted (type, key, layer, content_hash) list: "same set as last time?"

    def by_key(self, type_: str) -> dict[str, ResolvedAsset]:
        """The entries of one type by key. For a locked rule delivered with extensions the locked rule is the one returned."""
        out: dict[str, ResolvedAsset] = {}
        for e in self.entries:
            if e.type == type_ and (e.key not in out or e.locked):
                out[e.key] = e
        return out


def _job_kinds(job) -> set[str] | None:
    if job is None:
        return None
    if isinstance(job, str):
        return {job}
    if isinstance(job, dict):
        kinds = set(job.get("task_types") or [])
        if job.get("kind"):
            kinds.add(job["kind"])
        return kinds or None
    return None


def _entry(r: dict) -> ResolvedAsset:
    return ResolvedAsset(
        asset_id=r["id"], uid=r["uid"], type=r["type"], key=r["key"], name=r["name"], summary=r["summary"],
        layer=r["layer"], version_id=r["version_id"], n=r["n"], content_hash=r["content_hash"], pinned=r["pinned"],
        locked=r["locked"], extends=r["extends"],
        owner={"scope": r["owner_scope"], "project_id": r["owner_project_id"], "slug": r["owner_slug"],
               "name": r["owner_name"]},
        repo_path=r["repo_path"], created_at=r["created_at"])


def _apply_precedence(group: list[ResolvedAsset]) -> tuple[list[ResolvedAsset], list[ResolvedAsset], list[dict]]:
    """One (type, key) group sorted by layer: -> (kept, overridden, conflicts)."""
    locked = [e for e in group if e.locked]
    if locked:
        lock = min(locked, key=lambda e: e.layer)
        kept, over, conflicts = [lock], [], []
        for e in group:
            if e is lock:
                continue
            if e.layer < lock.layer:
                over.append(e)                  # a broader layer's version loses to the locked rule above it, as to any winner
            elif e.extends == lock.key:
                kept.append(e)                  # tightens the locked rule: both delivered
            else:
                over.append(e)
                conflicts.append({"type": lock.type, "key": lock.key, "reason": "locked", "dropped_asset_id": e.asset_id,
                                  "dropped_layer": e.layer, "locked_asset_id": lock.asset_id, "locked_layer": lock.layer})
        return kept, over, conflicts
    top = group[-1].layer
    tied = [e for e in group if e.layer == top]
    winner = tied[0]                      # query order is (shared_at, id): the earliest share wins a layer-3 tie
    conflicts = []
    if len(tied) > 1:
        conflicts.append({"type": winner.type, "key": winner.key, "reason": "tie", "layer": top,
                          "winner_asset_id": winner.asset_id, "other_asset_ids": [e.asset_id for e in tied if e is not winner]})
    return [winner], [e for e in group if e is not winner], conflicts


def resolve(project_id: int, *, types: tuple[str, ...] | list[str] | None = None, job=None, repo_commit: str | None = None,
            cur=None) -> ResolvedSet:
    """The assets that reach `project_id` and the version of each. See the module docstring for the rules."""
    args = {"pid": project_id, "types": list(types) if types else None, "commit": repo_commit}
    if cur is not None:
        cur.execute(_SQL, args)
        rows = cur.fetchall()
    else:
        with get_cursor(commit=False) as c:
            c.execute(_SQL, args)
            rows = c.fetchall()
    if not rows and not _project_exists(project_id, cur):
        raise NotFound(f"project {project_id} not found")
    kinds = _job_kinds(job)
    groups: dict[tuple[str, str], list[ResolvedAsset]] = {}
    kind_filter: dict[int, list] = {}
    for r in rows:
        e = _entry(r)
        groups.setdefault((e.type, e.key), []).append(e)
        kind_filter[e.asset_id] = r["job_kinds"] or []
    entries, over, conflicts, not_for_job = [], [], [], []
    for _, group in groups.items():
        kept, o, c = _apply_precedence(group)
        over += o
        conflicts += c
        for e in kept:
            wanted = kind_filter[e.asset_id]
            if kinds is not None and not e.locked and e.type in ("skill", "rule") and wanted and not kinds & set(wanted):
                not_for_job.append(e)
            else:
                entries.append(e)
    digest = hashlib.sha256("\n".join(f"{e.type}\t{e.key}\t{e.layer}\t{e.content_hash}"
                                      for e in sorted(entries, key=lambda e: (e.type, e.key, e.layer))).encode()).hexdigest()
    return ResolvedSet(project_id, tuple(entries), tuple(over), tuple(conflicts), tuple(not_for_job), digest)


def _project_exists(project_id: int, cur=None) -> bool:
    if cur is not None:
        cur.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,))
        return cur.fetchone() is not None
    with get_cursor(commit=False) as c:
        c.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,))
        return c.fetchone() is not None


def hydrate(entries, cur=None) -> dict[int, dict]:
    """{version_id: {"fields", "body", "files": [{path, sha256, bytes, content_type, rel_path}]}} for these entries, in two
    queries. The resolver leaves text out so a list of hundreds costs nothing; this is for delivery and the asset page."""
    ids = sorted({e.version_id for e in entries})
    if not ids:
        return {}

    def _run(c):
        c.execute("SELECT id, fields, body FROM asset_versions WHERE id = ANY(%s)", (ids,))
        out = {r["id"]: {"fields": r["fields"], "body": r["body"], "files": []} for r in c.fetchall()}
        c.execute("SELECT f.version_id, f.path, f.sha256, b.bytes, b.content_type, b.rel_path FROM asset_files f "
                  "JOIN asset_blobs b ON b.sha256 = f.sha256 WHERE f.version_id = ANY(%s) ORDER BY f.path", (ids,))
        for r in c.fetchall():
            out[r["version_id"]]["files"].append({k: r[k] for k in ("path", "sha256", "bytes", "content_type", "rel_path")})
        return out

    if cur is not None:
        return _run(cur)
    with get_cursor(commit=False) as c:
        return _run(c)
