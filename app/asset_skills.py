"""Skills as assets (task #718): the service behind the Skills pages, the CA's skill tools and delivery to runner jobs.

A skill is an asset of type `skill` (app/asset_types.py): a SKILL.md body, a description, optional extra files, and a few fields: `base`
(the skill it is built on), `license`, `source`. Layers, lowest to highest: built-in, global (instance), account, project; the resolver
(app/asset_resolve.py) picks the winner per key. Every save is a new immutable version (app/asset_store.py).

Rights are asked of app/asset_access.py HERE, so a route, a CA tool and a job can't forget them. Anything that needs no person
(seeding the built-ins, resolving a job's set) has its own function and takes no principal."""
from __future__ import annotations

import difflib
from datetime import datetime, timezone

from app import access, asset_access, asset_resolve, asset_store, asset_types, skill_md
from app.db import get_cursor
from app.repo import BadRequest, Forbidden, NotFound

LAYER_LABEL = {0: "Built-in", 1: "Global", 2: "Account", 3: "Shared", 4: "Project", 5: "Repo"}
MAX_BASE_DEPTH = 5
SKILL = "skill"


# --- what a version says ----------------------------------------------------------------------------------------------------

def licence_state(fields: dict) -> str:
    """'ok' (authored, permissive, or confirmed by a person) or 'blocked' (no licence found and nobody confirmed it)."""
    lic = (fields or {}).get("license") or {}
    if lic.get("status") in ("authored", "permissive") or lic.get("confirmed_by"):
        return "ok"
    return "blocked" if lic.get("status") == "unknown" else "ok"       # a skill with no licence field was written here


def is_imported(fields: dict) -> bool:
    return ((fields or {}).get("source") or {}).get("via") in ("github", "upload")


def hold_reason(fields: dict) -> str | None:
    """Why a skill is not given to jobs: 'licence' (no licence found, nobody confirmed), 'review' (imported, nobody has reviewed this
    version), or None. A skill written in Crewboss by a person is active at once."""
    if licence_state(fields) != "ok":
        return "licence"
    if is_imported(fields) and not (fields or {}).get("review"):
        return "review"
    return None


def usable(fields: dict) -> bool:
    return hold_reason(fields) is None


def _card(row: dict, **extra) -> dict:
    f = row["fields"] or {}
    lic = f.get("license") or {}
    return {"id": row["id"], "uid": row["uid"], "key": row["key"], "name": row["name"], "summary": row.get("summary", ""),
            "origin": row["origin"], "scope": row["owner_scope"], "n": row["n"], "version_id": row["version_id"],
            "description": f.get("description", ""), "base": f.get("base", ""), "license": lic, "source": f.get("source") or {},
            "usable": usable(f), "blocked": not usable(f), "hold": hold_reason(f), "imported": is_imported(f),
            "review": f.get("review") or {}, "files": row.get("file_count", 0), **extra}


_COLS = ("a.id, a.uid, a.key, a.name, a.summary, a.origin, a.owner_scope, a.owner_project_id, a.account_id, a.state, "
         "v.id AS version_id, v.n, v.fields, v.created_at AS version_at, v.created_by AS version_by")


# --- reads ------------------------------------------------------------------------------------------------------------------

def list_global(p) -> list[dict]:
    """The global layer: built-ins then the instance's own skills. Everyone signed in reads it."""
    if not asset_access.signed_in(p):
        raise NotFound("not found")
    with get_cursor(commit=False) as cur:
        cur.execute(f"SELECT {_COLS}, (SELECT count(*) FROM asset_files f WHERE f.version_id = v.id) AS file_count "
                    "FROM assets a JOIN asset_versions v ON v.id = a.current_version_id "
                    "WHERE a.type = 'skill' AND a.owner_scope = 'instance' AND a.state = 'active' "
                    "ORDER BY (a.origin = 'builtin') DESC, a.key")
        return [_card(r, layer=0 if r["origin"] == "builtin" else 1, layer_label=LAYER_LABEL[0 if r["origin"] == "builtin" else 1])
                for r in cur.fetchall()]


def _fields_by_version(ids) -> dict[int, dict]:
    if not ids:
        return {}
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT v.id, v.fields, (SELECT count(*) FROM asset_files f WHERE f.version_id = v.id) AS n FROM asset_versions v "
                    "WHERE v.id = ANY(%s)", (list(ids),))
        return {r["id"]: r for r in cur.fetchall()}


def list_for_project(p, project: dict) -> dict:
    """What a project's Skills page shows: {"entries": [card + layer, pinned, overridden], "mine": n}. Inherited and own, each marked
    with its layer. The person must be able to see the project."""
    access.require_view(p, project)
    rs = asset_resolve.resolve(project["id"], types=(SKILL,))
    over: dict[tuple, list] = {}
    for o in rs.overridden:
        over.setdefault(o.key, []).append(o)
    fields = _fields_by_version({e.version_id for e in rs.entries})
    out = []
    for e in rs.entries:
        fr = fields.get(e.version_id, {"fields": {}, "n": 0})
        row = {"id": e.asset_id, "uid": e.uid, "key": e.key, "name": e.name, "summary": e.summary, "origin": "builtin" if e.layer == 0 else "",
               "owner_scope": e.owner["scope"], "n": e.n, "version_id": e.version_id, "fields": fr["fields"], "file_count": fr["n"]}
        out.append(_card(row, layer=e.layer, layer_label=LAYER_LABEL[e.layer], pinned=e.pinned, owner=e.owner,
                         overrides=[LAYER_LABEL[o.layer] for o in over.get(e.key, [])]))
    out.sort(key=lambda c: (-c["layer"], c["key"]))
    return {"entries": out, "mine": sum(1 for c in out if c["layer"] in (4, 5) and (c["owner"]["project_id"] == project["id"])),
            "conflicts": list(rs.conflicts)}


def get(p, ref, *, n: int | None = None, via_project: dict | None = None) -> dict:
    """A skill (by uid) with a version, its files and tags. NotFound unless the person may see it: either its own layer is visible to
    them, or (`via_project`) it reaches a project they may see (an inherited skill from a layer above or a sibling they can't open)."""
    try:
        a = asset_store.get(ref, n=n)
    except NotFound:
        raise NotFound("no such skill") from None
    if a["type"] != SKILL:
        raise NotFound("no such skill")
    if asset_access.can_view(p, a):
        return a
    if via_project is not None and access.can_view(p, via_project["id"]):
        rs = asset_resolve.resolve(via_project["id"], types=(SKILL,))
        if a["id"] in {e.asset_id for e in rs.entries} | {e.asset_id for e in rs.overridden}:
            return a
    raise NotFound("no such skill")


def check_install_rights(p, install_to: dict | None) -> None:
    """Raises if the person may not install a skill there (before anything is fetched)."""
    _target(p, install_to)


def pin_of(project_id: int, asset_id: int) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT p.version_id, v.n, p.reason FROM asset_pins p JOIN asset_versions v ON v.id = p.version_id "
                    "WHERE p.project_id = %s AND p.asset_id = %s", (project_id, asset_id))
        return cur.fetchone()


def copy_to(p, ref, install_to: dict | None, key: str = "") -> dict:
    """Make your own copy of a skill (a built-in, or one from a layer above) in the chosen layer, with the same key unless one is given:
    the copy then overrides the original there and below. Licence and source carry over."""
    a = get(p, ref)
    v = a["version"]
    files = [{"path": f["path"], "raw": asset_store.read_rel(f["rel_path"])} for f in v["files"]]
    f = v["fields"] or {}
    return create(p, name=a["name"], key=key.strip() or a["key"], description=f.get("description", ""), body=v["body"], install_to=install_to,
                  base=f.get("base", ""), allowed_tools=f.get("allowed_tools"), files=files, license_=f.get("license"), source=f.get("source"),
                  review=f.get("review"), note=f"Copied from {a['key']} v{v['n']}"[:500])


def _need_active(a: dict) -> None:
    if a["state"] != "active":
        raise BadRequest("this skill is archived")


def versions(p, ref) -> list[dict]:
    a = get(p, ref)
    return asset_store.versions(a["id"])


def _diff_text(v: dict, key: str) -> str:
    f = v["fields"] or {}
    head = [f"description: {f.get('description', '')}", f"based on: {f.get('base') or '(none)'}",
            "files: " + (", ".join(x["path"] for x in v.get("files", [])) or "(none)"), "---"]
    return "\n".join(head) + "\n" + (v["body"] or "")


def diff(p, ref, a: int, b: int) -> list[dict]:
    """The lines that changed between version a and version b: [{"kind": "add" | "del" | "ctx" | "hdr", "text"}]."""
    x, y = get(p, ref, n=a), get(p, ref, n=b)
    lines = list(difflib.unified_diff(_diff_text(x["version"], x["key"]).splitlines(), _diff_text(y["version"], y["key"]).splitlines(),
                                      f"v{a}", f"v{b}", lineterm="", n=2))
    out = []
    for ln in lines:
        kind = "hdr" if ln.startswith(("---", "+++", "@@")) else "add" if ln.startswith("+") else "del" if ln.startswith("-") else "ctx"
        out.append({"kind": kind, "text": ln if kind == "hdr" else ln[1:]})
    return out


# --- bases ------------------------------------------------------------------------------------------------------------------

def _scope_fields(project: dict | None) -> dict[str, dict]:
    """{skill key: current fields} of the skills a skill in this scope may be built on (global: the global ones; a project: what reaches it)."""
    with get_cursor(commit=False) as cur:
        if project is None:
            cur.execute("SELECT a.key, v.fields FROM assets a JOIN asset_versions v ON v.id = a.current_version_id "
                        "WHERE a.type = 'skill' AND a.owner_scope = 'instance' AND a.state = 'active' "
                        "ORDER BY (a.origin = 'builtin') DESC")       # a global of the same key overrides the built-in
            return {r["key"]: r["fields"] for r in cur.fetchall()}
    rs = asset_resolve.resolve(project["id"], types=(SKILL,))
    f = _fields_by_version({e.version_id for e in rs.entries})
    return {e.key: f[e.version_id]["fields"] for e in rs.entries}


def check_base(project: dict | None, key: str, base: str | None) -> None:
    """The base must be a skill that reaches this scope, must not be the skill itself, and the chain must not loop or run deep."""
    if not base:
        return
    avail = _scope_fields(project)
    if base == key:
        raise BadRequest("a skill can't be built on itself")
    seen, cur_key = [key], base
    for _ in range(MAX_BASE_DEPTH + 1):
        if cur_key not in avail:
            raise BadRequest(f"there is no skill called {cur_key} to build on here")
        if cur_key in seen:
            raise BadRequest("that would make a loop: the base skill is already built on this one")
        seen.append(cur_key)
        cur_key = (avail[cur_key] or {}).get("base") or ""
        if not cur_key:
            return
    raise BadRequest(f"skills can be built on one another at most {MAX_BASE_DEPTH} deep")


# --- writes -----------------------------------------------------------------------------------------------------------------

def _target(p, install_to: dict | None) -> tuple[str, int | None, bool]:
    """(owner_scope, owner_project_id, share_all) for 'Install to'. None = global (instance owner only); an account = every project
    in it; any other project = that project."""
    if install_to is None:
        if not asset_access.can_install_global(p):
            raise Forbidden("only the owner installs a skill globally")
        return "instance", None, True
    access.require_edit(p, install_to)
    if install_to["kind"] == "account":
        return "account", install_to["id"], True
    return "project", install_to["id"], False


def _files(files) -> list[dict]:
    return [{"path": f["path"], "raw": f["raw"]} for f in (files or [])]


def create(p, *, name: str, key: str = "", description: str, body: str, install_to: dict | None = None, base: str = "",
           allowed_tools=None, files=None, note: str = "", via: str = "web", license_: dict | None = None,
           source: dict | None = None, origin: str = "authored", review: dict | None = None) -> dict:
    """A new skill: version 1. `install_to` None = Global."""
    key = key.strip() or skill_md.slugify(name)
    if not skill_md.KEY_RE.match(key):
        raise BadRequest("the skill's key is lower-case letters, numbers and dashes")
    description = " ".join((description or "").split())
    if not description:
        raise BadRequest("a skill needs a description: what it is for and when to use it")
    if not (body or "").strip():
        raise BadRequest("a skill needs its text (the SKILL.md body)")
    scope, owner_id, share_all = _target(p, install_to)
    check_base(install_to, key, base or None)
    fields: dict = {"description": description, "license": license_ or {"status": "authored"}}
    if base:
        fields["base"] = base
    if allowed_tools:
        fields["allowed_tools"] = list(allowed_tools)
    if source:
        fields["source"] = source
    if review:
        fields["review"] = review
    return asset_store.create(SKILL, key, name or key, scope, owner_id, fields, body, _files(files), summary=description[:500],
                              origin=origin, share_all=share_all, actor=asset_access.actor(p), via=via, note=note,
                              user_id=p.user_id)


def import_parsed(p, parsed, install_to: dict | None = None) -> dict:
    """Store a ParsedSkill (app/skill_import.py). An unknown licence is saved flagged, and every imported version waits for a person's
    review: neither is given to jobs until confirmed. Importing a key that is already an import in that place saves a NEW version
    (so it needs review again, with a diff from the last reviewed one)."""
    lic = dict(parsed.license)
    scope, owner_id, _share = _target(p, install_to)
    again = asset_store.find(SKILL, parsed.key, owner_id, origin="import")
    if again is not None and again["owner_scope"] == scope and again["state"] == "active":
        asset_access.require_edit(p, again)
        old = again["version"]["fields"]
        fields = {**parsed.fields(), "license": {**lic}}
        if old.get("base"):
            fields["base"] = old["base"]
        out = asset_store.new_version(again["id"], fields, parsed.body, _files(parsed.files), note=f"Re-imported from {parsed.source.get('url') or 'an upload'}"[:500],
                                      actor=asset_access.actor(p), via="import", user_id=p.user_id, summary=parsed.description[:500])
        return out
    return create(p, name=parsed.name, key=parsed.key, description=parsed.description, body=parsed.body, install_to=install_to,
                  allowed_tools=parsed.allowed_tools, files=parsed.files, via="import", license_=lic, source=parsed.source,
                  origin="import", note=f"Imported from {parsed.source.get('url') or 'an upload'}"[:500])


def edit(p, ref, *, description: str | None = None, body: str | None = None, base: str | None = None, add_files=None,
         remove_files=(), note: str = "", base_version_id: int | None = None, via: str = "web", approved_user_id: int | None = None) -> dict:
    """Save a skill as its next version. Fields not passed stay as they were; the licence and source always carry over. An edit that
    changes nothing is a no-op (`created` False). Needs the right to change the skill."""
    a = get(p, ref)
    asset_access.require_edit(p, a)
    _need_active(a)
    if a["origin"] == "builtin":
        raise BadRequest("a built-in skill ships with Crewboss and can't be edited; install your own skill with the same key to replace it")
    v = a["version"]
    fields = dict(v["fields"])
    if description is not None:
        d = " ".join(description.split())
        if not d:
            raise BadRequest("a skill needs a description")
        fields["description"] = d
    if base is not None:
        project = None if a["owner_scope"] == "instance" else _project_row(a["owner_project_id"])
        check_base(project, a["key"], base or None)
        if base:
            fields["base"] = base
        else:
            fields.pop("base", None)
    new_body = v["body"] if body is None else body
    if not (new_body or "").strip():
        raise BadRequest("a skill needs its text (the SKILL.md body)")
    keep = [{"path": f["path"], "sha256": f["sha256"]} for f in v["files"] if f["path"] not in set(remove_files or ())]
    added = _files(add_files)
    names = {f["path"] for f in added}
    files = [f for f in keep if f["path"] not in names] + added
    return asset_store.new_version(a["id"], fields, new_body, files, base_version_id=base_version_id, note=note,
                                   actor=asset_access.actor(p), via=via, user_id=p.user_id, approved_user_id=approved_user_id,
                                   summary=fields["description"][:500])


def _project_row(project_id: int) -> dict:
    from app import repo
    return repo.get_project(project_id)


def confirm_licence(p, ref, note: str = "") -> dict:
    """An admin says: this skill's licence is fine to use. A new version (so it is on the record) with the person and the date."""
    a = get(p, ref)
    if not asset_access.can_confirm_licence(p, a):
        raise Forbidden("only an admin confirms a licence")
    lic = dict((a["version"]["fields"].get("license") or {}))
    if lic.get("status") != "unknown":
        raise BadRequest("this skill's licence needs no confirmation")
    if lic.get("confirmed_by"):
        raise BadRequest("this licence is already confirmed")
    # confirmed_by is the person's users id; -1 = the console owner, who has no users row
    lic.update(confirmed_by=p.user_id or -1, confirmed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    fields = dict(a["version"]["fields"], license=lic)
    return asset_store.new_version(a["id"], fields, a["version"]["body"], "keep", note=(note or "Licence confirmed")[:500],
                                   actor=asset_access.actor(p), via="web", user_id=p.user_id, approved_user_id=p.user_id)


def review(p, ref, note: str = "") -> dict:
    """A person who may confirm licences says: I read this imported version and jobs may use it. A new version carrying who and when."""
    a = get(p, ref)
    if not asset_access.can_confirm_licence(p, a):
        raise Forbidden("only an admin reviews an imported skill")
    f = dict(a["version"]["fields"])
    if not is_imported(f):
        raise BadRequest("only an imported skill needs a review")
    if f.get("review"):
        raise BadRequest("this version is already reviewed")
    f["review"] = {"reviewed_by": p.user_id or -1, "reviewed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return asset_store.new_version(a["id"], f, a["version"]["body"], "keep", note=(note or "Reviewed")[:500], actor=asset_access.actor(p),
                                   via="web", user_id=p.user_id, approved_user_id=p.user_id)


def last_reviewed_n(p, ref) -> int | None:
    """The newest version number (below the live one) that a person reviewed, to show what changed since."""
    for v in asset_store.versions(get(p, ref)["id"]):
        if (v["fields"] or {}).get("review"):
            return v["n"]
    return None


def archive(p, ref, reason: str = "") -> dict:
    a = get(p, ref)
    asset_access.require_edit(p, a)
    if a["origin"] == "builtin":
        raise BadRequest("a built-in skill can't be archived; install your own with the same key to replace it")
    return asset_store.archive(a["id"], asset_access.actor(p), reason, p.user_id)


def pin(p, project: dict, ref, n: int, reason: str = "") -> None:
    """Hold an inherited skill at version n in this project. Needs the right to change the project; the skill must reach it."""
    access.require_edit(p, project)
    a = get(p, ref)
    rs = asset_resolve.resolve(project["id"], types=(SKILL,))
    if a["id"] not in {e.asset_id for e in rs.entries} | {e.asset_id for e in rs.overridden}:
        raise NotFound("no such skill")
    asset_store.pin(a["id"], project["id"], n, reason, p.user_id)


def unpin(p, project: dict, ref) -> None:
    access.require_edit(p, project)
    a = get(p, ref)
    asset_store.unpin(a["id"], project["id"])


# --- delivery: what a job gets -----------------------------------------------------------------------------------------------

def compose(key: str, by_key: dict[str, dict]) -> tuple[str, list[dict], list[str], list[str]]:
    """The skill's FULL text: its base chain (oldest base first) then its own body, files merged (the skill's own win), tools unioned.
    `by_key` = {key: {"fields", "body", "files": [...]}} of the skills that reach the job. -> (body, files, allowed_tools, warnings)"""
    chain, seen, warnings = [], set(), []
    cur = key
    while cur and cur not in seen and len(chain) <= MAX_BASE_DEPTH:
        entry = by_key.get(cur)
        if entry is None:
            warnings.append(f"the base skill {cur} did not reach this job")
            break
        if cur != key and not usable(entry["fields"]):
            warnings.append(f"the base skill {cur} is held back: its licence is not confirmed")
            break
        seen.add(cur)
        chain.append(entry)
        cur = (entry["fields"] or {}).get("base") or ""
    if cur and cur in seen:
        warnings.append("a loop of base skills was cut")
    chain.reverse()
    bodies = [c["body"].strip("\n") for c in chain if (c["body"] or "").strip()]
    files: dict[str, dict] = {}
    tools: list[str] = []
    for c in chain:
        for f in c["files"]:
            files[f["path"]] = f
        for t in (c["fields"] or {}).get("allowed_tools") or []:
            if t not in tools:
                tools.append(t)
    return "\n\n".join(bodies), list(files.values()), tools, warnings
