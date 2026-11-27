"""Catalog import — ONE code path for every way a scan arrives:

    python -m app.catalog_import <file.json> [--project acme-app]      (CLI, e.g. after `docker cp`)
    POST /api/catalog/import?project=acme-app   (JSON body = the catalog; scanner/auto_rescan.py)

Both call import_document(), which checks the document is shaped like a catalog (docs/catalog-schema.md)
and hands it to repo.import_catalog — that stores it as a new catalog_scans row, diffs it against the
project's previous scan into catalog_diffs, and raises the dedup'd Tweak tasks for stale and broken
workflows. An optional top-level `integrations` (the ERP's graded integration list, #393) is shape-checked and rides
along in the same scan row; after the import, the ERP project's catalog is also written to exports/catalog.json for
the ERP's admin site (app/catalog_export.py)."""
from __future__ import annotations

import argparse
import json
import sys

from app import catalog_export, instance, repo
from app import integrations as integrations_mod
from app.repo import BadRequest

DEFAULT_PROJECT = instance.get_str("catalog_project")


def import_document(catalog, project: str = DEFAULT_PROJECT) -> dict:
    """Validate the top-level shape, then import. Raises repo.BadRequest (→ HTTP 400) on a document
    that isn't a catalog and repo.NotFound (→ 404) on an unknown project."""
    check_document(catalog)
    result = repo.import_catalog(project, catalog)
    catalog_export.write(catalog, project)
    return result


def check_document(catalog) -> None:
    """The import-side checks, shared with the offline export CLI (python -m app.catalog_export, #402) so a catalog it
    accepts is one the live import would accept. Raises BadRequest. Deep validation is the scanner's job
    (scanner/lib/schema.py, which refuses to emit an invalid catalog under --check); this guards against a wrong
    file/body being stored as a scan. Touches no database."""
    if not isinstance(catalog, dict):
        raise BadRequest("catalog must be a JSON object")
    workflows = catalog.get("workflows")
    if not isinstance(workflows, list):
        raise BadRequest("catalog.workflows must be a list")
    if not isinstance(catalog.get("commit"), str) or not catalog.get("generated_at"):
        raise BadRequest("catalog needs 'commit' and 'generated_at'")
    for i, wf in enumerate(workflows):
        if not isinstance(wf, dict) or not isinstance(wf.get("id"), str) or not isinstance(wf.get("name"), str):
            raise BadRequest(f"catalog.workflows[{i}] needs a string 'id' and 'name'")
    if "integrations" in catalog:
        integrations_mod.validate(catalog["integrations"])


def summary_line(result: dict) -> str:
    return (f"imported scan #{result['scan_id']}: {result['workflow_count']} workflows, "
            f"{result['diff_count']} changed since the previous scan, "
            f"{result.get('stale_count', 0)} newly stale, {result.get('broken_count', 0)} broken")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.catalog_import")
    parser.add_argument("file", help="scanner output JSON file")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args(argv)

    with open(args.file, encoding="utf-8") as fh:
        catalog = json.load(fh)

    result = import_document(catalog, args.project)
    print(summary_line(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
