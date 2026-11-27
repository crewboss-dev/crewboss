"""First-run seed: one project (and optional tasks) so a fresh install has something to open. Idempotent: safe to run on
every start.

python -m app.seed

An install's own seed lives in CREWBOSS_HOME/seed.yaml (see seed.example.yaml); without one, a single generic example
project is created. No install's names live in this file.
"""
from __future__ import annotations

import logging

import yaml
from psycopg.types.json import Jsonb

from app import instance, repo
from app.db import get_cursor

log = logging.getLogger("crewboss.seed")

DEFAULT_SPEC = {
    "project": {"slug": "example", "name": "Example project", "kind": "internal", "owner": "system",
                "short_name": "example"},
    "subprojects": [],
    "tasks": [],
}


def load_spec(path: str | None = None) -> dict:
    """seed.yaml from the instance folder, else the generic default. A malformed file is a loud error."""
    path = path or instance.path("seed.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except FileNotFoundError:
        return DEFAULT_SPEC
    if not isinstance(doc, dict) or not isinstance(doc.get("project"), dict) or not doc["project"].get("slug"):
        raise ValueError(f"{path}: seed.yaml must be a mapping with a `project:` that has a slug")
    return {"project": {**DEFAULT_SPEC["project"], "name": doc["project"]["slug"], **doc["project"]},
            "subprojects": [str(x) for x in doc.get("subprojects") or []],
            "tasks": list(doc.get("tasks") or [])}


def run(spec: dict | None = None) -> None:
    spec = spec or load_spec()
    p = spec["project"]
    with get_cursor() as cur:
        cur.execute("SELECT id FROM projects WHERE slug = %s", (p["slug"],))
        row = cur.fetchone()
    if row:
        project_id = row["id"]
    else:
        project = repo.create_project(p["slug"], p["name"], p["kind"], p["owner"], short_name=p["short_name"],
                                      session_roles=list(repo.SESSION_STREAMS))
        project_id = project["id"]
        print(f"created project {p['name']!r} (id={project_id})")

    for name in spec["subprojects"]:
        repo.add_subproject(project_id, name)

    tasks = repo.list_tasks(project_id)
    seeded_keys = {(t["links"] or {}).get("seed_key") for t in tasks}
    for t in spec["tasks"]:
        seed_key, title = t["seed_key"], t["title"]
        if seed_key in seeded_keys:
            continue
        # One-time adoption: a copy seeded before seed_key existed is matched by its ORIGINAL title
        # and stamped, instead of being duplicated.
        legacy = next((x for x in tasks if x["title"] == title and not (x["links"] or {}).get("seed_key")), None)
        if legacy:
            with get_cursor() as cur:
                cur.execute("UPDATE tasks SET links = links || %s WHERE id = %s",
                            (Jsonb({"seed_key": seed_key}), legacy["id"]))
            continue
        repo.create_task(project_id, title, str(t.get("body") or "").strip(), t.get("priority", "should"),
                         t.get("stream", "bug"), list(t.get("subprojects") or []), {"seed_key": seed_key},
                         t.get("actor", "owner"))
        print(f"seeded task: {title}")


if __name__ == "__main__":
    run()
