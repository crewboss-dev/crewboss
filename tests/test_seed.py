"""Nit: a seeded task is keyed on links.seed_key, so renaming it never duplicates it. The seed itself comes from
CREWBOSS_HOME/seed.yaml (a spec here), or is one generic example project."""
import pytest

from app import repo, seed
from app.db import get_cursor

SPEC = {
    "project": {"slug": "acme-erp", "name": "Acme App", "kind": "internal", "owner": "system", "short_name": "app"},
    "subprojects": ["Backend"],
    "tasks": [{"seed_key": "acme-backups", "title": "Set up backups", "stream": "bug", "priority": "must",
               "subprojects": ["Backend"], "actor": "owner", "body": "A real first task."}],
}


def _seeded(project_id):
    return [t for t in repo.list_tasks(project_id) if (t["links"] or {}).get("seed_key") == "acme-backups"]


def _project_id():
    return repo.get_project_by_slug("acme-erp")["id"]


def test_seed_is_idempotent_and_rename_safe():
    seed.run(SPEC)
    pid = _project_id()
    [task] = _seeded(pid)
    repo.update_task(task["id"], {"title": "Back up the dev box"}, "owner")
    repo.update_task(task["id"], {"links": {"workflows": [{"id": "x"}]}}, "claude:bugs")
    seed.run(SPEC)
    assert len(repo.list_tasks(pid)) == 1
    [task] = _seeded(pid)
    assert task["title"] == "Back up the dev box"
    assert task["links"]["workflows"] == [{"id": "x"}]


def test_seed_adopts_a_legacy_copy_without_seed_key():
    seed.run(SPEC)
    pid = _project_id()
    with get_cursor() as cur:  # as the live DB has it: seeded by title, before seed_key existed
        cur.execute("UPDATE tasks SET links = '{}'::jsonb")
    seed.run(SPEC)
    assert len(repo.list_tasks(pid)) == 1
    assert len(_seeded(pid)) == 1


def test_without_a_seed_file_one_generic_example_project_is_made(tmp_path):
    spec = seed.load_spec(str(tmp_path / "seed.yaml"))
    assert spec["project"]["slug"] == "example" and spec["tasks"] == []
    seed.run(spec)
    seed.run(spec)
    assert repo.get_project_by_slug("example")["name"] == "Example project"


def test_a_seed_file_is_read_from_the_instance_folder(tmp_path):
    (tmp_path / "seed.yaml").write_text("project: {slug: acme-erp, name: Acme App}\nsubprojects: [Backend]\n")
    spec = seed.load_spec(str(tmp_path / "seed.yaml"))
    assert spec["project"]["slug"] == "acme-erp" and spec["project"]["kind"] == "internal" and spec["subprojects"] == ["Backend"]
    (tmp_path / "bad.yaml").write_text("- nope\n")
    with pytest.raises(ValueError, match="bad.yaml"):
        seed.load_spec(str(tmp_path / "bad.yaml"))


def test_the_example_seed_file_loads():
    from pathlib import Path
    spec = seed.load_spec(str(Path(__file__).resolve().parent.parent / "seed.example.yaml"))
    assert spec["project"]["slug"] == "acme-app" and spec["tasks"][0]["seed_key"] == "acme-first-task"
