"""Integrations (#393, the dashboard side of ERP #391): every link between the ERP and another system, graded A–D,
read from the ERP's own export (app/integrations_registry.py export(), committed by the ERP as
docs/generated/integrations.json). The scanner reads that file from the ERP's origin/main and carries it in the catalog
it imports, as the top-level `integrations` (docs/catalog-schema.md), so it arrives the same way workflows do: the
scanner role token, one project's scan, nothing stored beyond the scan itself. A project whose latest scan has none
(every other account's, or the ERP's before the file reached its main) shows an empty state.

Owner decision q208: grades ARE shown on the Dashboard. Key NAMES only; the export has no key state and no values.

A step uses integrations two ways: its own `integrations` ids (an annotation narrowing it), else every id its `system`
maps to in the export's `systems`. resolve_step() is the one rule; the canvas bundle applies the same one
(frontend/src/canvas/integrations-model.ts)."""
from __future__ import annotations

import re

from app import repo
from app.repo import BadRequest

GRADES = ("A", "B", "C", "D")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_REQUIRED = ("id", "name", "module", "peer", "zone", "flow", "kind", "auth", "transport", "grade")
_TEXT = ("id", "name", "module", "purpose", "us", "peer", "zone", "flow", "starts", "kind", "label", "endpoint",
         "resolves", "path", "auth", "transport", "notes", "status", "accepted", "grade")


def validate(doc) -> None:
    """Shape-check an export before it is stored with a scan. Raises BadRequest (→ 400). Deep correctness is the
    ERP's (its tests hold the registry to its code); this guards against a wrong file being stored as one."""
    if not isinstance(doc, dict):
        raise BadRequest("catalog.integrations must be an object")
    if doc.get("version") != 1:
        raise BadRequest("catalog.integrations.version must be 1")
    rows = doc.get("integrations")
    if not isinstance(rows, list):
        raise BadRequest("catalog.integrations.integrations must be a list")
    seen = set()
    for n, r in enumerate(rows):
        where = f"catalog.integrations.integrations[{n}]"
        if not isinstance(r, dict):
            raise BadRequest(f"{where} must be an object")
        for k in _REQUIRED:
            if not isinstance(r.get(k), str) or not r[k]:
                raise BadRequest(f"{where} needs a string '{k}'")
        for k in _TEXT:
            if k in r and not isinstance(r[k], str):
                raise BadRequest(f"{where}.{k} must be a string")
        if not _ID_RE.match(r["id"]):
            raise BadRequest(f"{where}.id {r['id']!r} isn't a plain id")
        if r["id"] in seen:
            raise BadRequest(f"duplicate integration id {r['id']!r}")
        seen.add(r["id"])
        if r["grade"] not in GRADES:
            raise BadRequest(f"{where}.grade must be one of {', '.join(GRADES)}")
        if not isinstance(r.get("why", []), list) or not all(isinstance(w, str) for w in r.get("why", [])):
            raise BadRequest(f"{where}.why must be a list of strings")
        if not isinstance(r.get("keys", []), list) or not all(isinstance(k, str) for k in r.get("keys", [])):
            raise BadRequest(f"{where}.keys must be a list of key NAMES")
    systems = doc.get("systems", {})
    if not isinstance(systems, dict):
        raise BadRequest("catalog.integrations.systems must be an object")
    for system, ids in systems.items():
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise BadRequest(f"catalog.integrations.systems[{system!r}] must be a list of ids")
        unknown = [i for i in ids if i not in seen]
        if unknown:
            raise BadRequest(f"catalog.integrations.systems[{system!r}] names unknown ids: {', '.join(unknown)}")
    vocab = doc.get("vocab", {})
    if not isinstance(vocab, dict) or not all(isinstance(v, dict) for v in vocab.values()):
        raise BadRequest("catalog.integrations.vocab must be an object of label maps")


def latest(project_id: int) -> dict | None:
    """The export carried by the project's latest scan, or None (no scan, or a scan without one)."""
    scan = repo.latest_scan(project_id)
    doc = (scan["catalog"] or {}).get("integrations") if scan else None
    return doc if isinstance(doc, dict) else None


def sources(project: dict) -> list[tuple[str, dict]]:
    """Every integrations list the project's page shows, keyed by its SOURCE (the project whose scan carried it).
    Today that is the project's own scan — the ERP's list. The dashboard's own integrations (#398: Zabbix,
    Wazuh, the ERP feeds, Entra, …) will arrive the same way, as another source with the same export shape and its own
    module name ("dashboard"), and join the page through merged() without the page changing."""
    from app import integrations_dashboard

    doc = latest(project["id"])
    out = [(project["slug"], doc)] if doc else []
    if integrations_dashboard.shown_on(project):      # the Dashboard's own links (#562), on its own project
        out.append((integrations_dashboard.SOURCE, integrations_dashboard.export()))
    return out


def merged(srcs: list[tuple[str, dict]]) -> dict | None:
    """One export over several sources: every row tagged with its `source`, the vocabularies merged (first wins). The
    `systems` map stays per source (a step resolves against its own project's list only), so it is left out."""
    if not srcs:
        return None
    vocab: dict[str, dict] = {}
    modules: dict[str, str] = {}
    rows: list[dict] = []
    for source, doc in srcs:
        for key, name in (doc.get("modules") or {}).items():   # module key -> display name (optional)
            modules.setdefault(key, name)
        for group, labels in (doc.get("vocab") or {}).items():
            vocab.setdefault(group, {})
            for code, label in labels.items():
                vocab[group].setdefault(code, label)
        rows += [dict(r, source=source) for r in doc.get("integrations") or []]
    rows = by_weakest(rows)
    return {"version": 1, "vocab": vocab, "modules": modules, "integrations": rows, "systems": {},
            "summary": summary(rows), "sources": [source for source, _ in srcs]}


def _rank(grade: str) -> int:
    return GRADES.index(grade) if grade in GRADES else -1


def by_weakest(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (-_rank(r.get("grade", "")), r.get("name", "").lower()))


def step_ids(step: dict, doc: dict | None) -> list[str]:
    """The ids a step names (non-empty `integrations`), else those its system maps to — unknown ids included, so a
    check can report them."""
    own = step.get("integrations")
    if isinstance(own, list) and own:
        ids = [i for i in own if isinstance(i, str)]
    else:
        ids = list(((doc or {}).get("systems") or {}).get(step.get("system") or "", []))
    return list(dict.fromkeys(ids))


def resolve_step(step: dict, doc: dict | None) -> list[str]:
    """The integration ids a step uses, KNOWN ones only, weakest grade first."""
    rows = {r["id"]: r for r in (doc or {}).get("integrations") or []}
    return [r["id"] for r in by_weakest([rows[i] for i in step_ids(step, doc) if i in rows])]


def summary(rows: list[dict]) -> dict[str, int]:
    """Count per grade — every grade present, zero included, so an empty grade still renders as 0."""
    return {g: sum(1 for r in rows if r.get("grade") == g) for g in GRADES}


def modules(rows: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["module"]] = counts.get(r["module"], 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def subset_for_workflow(wf: dict, doc: dict | None) -> dict | None:
    """The slice of the export a workflow canvas needs: the vocab, the rows its steps use, and the systems map
    trimmed to those rows — the page embeds this, not the whole list."""
    if not doc:
        return None
    rows = {r["id"]: r for r in doc.get("integrations") or []}
    used: list[str] = []
    for step in wf.get("steps") or []:
        for i in resolve_step(step, doc):
            if i not in used:
                used.append(i)
    systems = {s: [i for i in ids if i in used] for s, ids in (doc.get("systems") or {}).items()}
    return {"version": doc.get("version", 1), "vocab": doc.get("vocab") or {},
            "integrations": [rows[i] for i in used], "systems": {s: ids for s, ids in systems.items() if ids}}
