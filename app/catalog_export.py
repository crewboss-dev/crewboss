"""The catalog export for the ERP (#393): on every import of the ERP project's catalog, exports/catalog.json in the
dashboard checkout (bind-mounted into the app at CREWBOSS_EXPORTS_DIR) gets the workflows and the AI skills in the
catalog-schema format, each step with its resolved `integrations: [ids]`. The ERP's release script copies it, so its
admin site draws the same workflows with the shared canvas (docs/canvas-contract.md). A release input, not source:
exports/ is git-ignored.

Same PRIVACY RULE as the catalog: built from the scan only (code-derived), never runtime data.

The SAME file offline (#402, no drift): the ERP's release-prep builds its admin site's catalog for the exact commit it
ships, without the live dashboard:

    .venv/bin/python -m app.catalog_export --project acme-app --ref <sha> --out <file>
    .venv/bin/python -m app.catalog_export --project acme-app --erp-tree <dir> --commit <sha> --out <file>

It scans that ref the way scanner/auto_rescan.py does (git archive, the same annotations.yaml), runs the import's own
checks (catalog_import.check_document) and writes build() through render(), the one path the live write uses too, so
the two can't diverge. Pure offline: no git fetch, no dashboard, no database. Exit 0 = written; 1 = the catalog would
be refused (schema, privacy sweep, unknown integration ids, no docs/generated/integrations.json at that ref); 2 = bad
arguments or git couldn't resolve/export the ref."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import config, roles
from app import integrations as integrations_mod
from app.skills import derive_skills

log = logging.getLogger(__name__)
FILENAME = "catalog.json"
VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build(catalog: dict) -> dict:
    """{version, project, commit, generated_at, built_at, integrations_version, workflows, skills}. Steps gain
    `integrations` (known ids only, weakest first; [] when none) — the catalog itself is left as the scanner wrote it.
    `commit` is the ERP commit the catalog was scanned from; `built_at` is when this export was made."""
    doc = catalog.get("integrations") if isinstance(catalog.get("integrations"), dict) else None
    workflows = []
    for wf in catalog.get("workflows") or []:
        wf = dict(wf)
        wf["steps"] = [dict(s, integrations=integrations_mod.resolve_step(s, doc)) for s in wf.get("steps") or []]
        workflows.append(wf)
    skills = []
    for s in derive_skills(catalog.get("workflows") or []):
        skills.append({k: v for k, v in s.items()})
    return {"version": VERSION, "project": catalog.get("project"), "commit": catalog.get("commit"),
            "generated_at": catalog.get("generated_at"), "built_at": _now(),
            "integrations_version": (doc or {}).get("version"), "workflows": workflows, "skills": skills}


def render(catalog: dict) -> str:
    """The file's exact text — the live write and the CLI both go through here."""
    return json.dumps(build(catalog), indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def write(catalog: dict, project_slug: str) -> Path | None:
    """Write exports/catalog.json for the export project (the ERP); any other project, or no exports dir configured,
    is a no-op. Never fails an import: a write error is logged and the import stands."""
    out_dir = config.EXPORTS_DIR
    if not out_dir or project_slug != roles.slug("export"):
        return None
    try:
        d = Path(out_dir)
        if not d.is_dir():
            log.warning("catalog export: %s is not a directory — skipped", d)
            return None
        target = d / FILENAME
        tmp = d / (FILENAME + ".tmp")
        tmp.write_text(render(catalog), encoding="utf-8")
        os.chmod(tmp, 0o644)
        st = d.stat()
        try:   # the app runs as root in its container: hand the file to whoever owns the host folder
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, target)
        return target
    except OSError as exc:
        log.error("catalog export: couldn't write %s: %s", out_dir, exc)
        return None


# ---------------------------------------------------------------------------------------------
# the offline CLI (#402)
# ---------------------------------------------------------------------------------------------

class ExportError(Exception):
    """A refusal, with the exit code to leave with (1 = the catalog would be refused, 2 = usage / git)."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise ExportError(f"git {' '.join(args)} in {repo} failed: {r.stderr.strip()[-300:]}", code=2)
    return r.stdout.strip()


def scan_offline(project: str, ref: str | None = None, repo: str | None = None, tree: str | None = None,
                 commit: str | None = None, annotations: str | None = None,
                 linkedin_repo: str | None = None, knowledge_repo: str | None = None,
                 companion_ref: str | None = None) -> dict:
    """The catalog for one ERP ref (or an already-exported tree), as auto_rescan would import it, after the import's
    own checks. Raises ExportError. The companion repos (LinkedIn, the host-automation repo) are read at their LOCAL
    origin/main, never fetched: an unavailable one is skipped, as auto_rescan's scan would."""
    from app import catalog_import
    from app.repo import BadRequest
    from scanner import scan as scanmod
    from scanner.lib import integrations as integrations_lib

    if project not in scanmod.PROJECT_REPOS:
        raise ExportError(f"the scanner can't build a catalog for project {project!r} "
                          f"(it builds: {', '.join(sorted(scanmod.PROJECT_REPOS))})", code=2)
    companion_ref = companion_ref or scanmod.DEFAULT_REF
    from scanner import config as scanner_config
    annotations = annotations or scanner_config.annotations_path()
    cleanup: list[Path] = []
    try:
        if tree:
            root = Path(tree)
            if not root.is_dir():
                raise ExportError(f"--erp-tree {tree} is not a directory", code=2)
            sha = commit or (_git(tree, "rev-parse", "HEAD") if (root / ".git").exists() else None)
            if not sha:
                raise ExportError("an exported tree has no commit of its own: pass --commit <sha>", code=2)
        else:
            if not ref:
                raise ExportError("pass --ref <git ref or sha> (or --erp-tree <dir> --commit <sha>)", code=2)
            repo = repo or scanmod.PROJECT_REPOS[project]
            sha = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
            try:
                root, sha = scanmod.export_repo(repo, sha, fetch=False)
            except subprocess.CalledProcessError as exc:
                raise ExportError(f"git archive of {sha} in {repo} failed: {exc}", code=2) from exc
            cleanup.append(root)
        companions = {}
        for name, path in (("linkedin", linkedin_repo), ("knowledge", knowledge_repo)):
            if path and Path(path, ".git").exists():
                try:
                    companions[name] = scanmod.export_repo(path, companion_ref, fetch=False)
                    cleanup.append(companions[name][0])
                except subprocess.CalledProcessError:
                    print(f"warning: couldn't export {path} at {companion_ref}: skipped", file=sys.stderr)
        if not (root / integrations_lib.PATH).is_file():
            raise ExportError(f"{integrations_lib.PATH} isn't in the ERP at {sha[:12]}: the admin catalog needs the "
                              f"integrations list of the same commit")
        li_root, li_sha = companions.get("linkedin", (None, None))
        kn_root, kn_sha = companions.get("knowledge", (None, None))
        result = scanmod.scan_trees(root, li_root, sha, li_sha, annotations, knowledge_root=kn_root,
                                    knowledge_sha=kn_sha, project=project)
    finally:
        for p in cleanup:
            shutil.rmtree(p, ignore_errors=True)

    refusals = [f"SCHEMA: {p}" for p in result.problems]
    # privacy findings quote what they matched: count them, never print the text
    if result.privacy_findings:
        refusals.append(f"PRIVACY: {len(result.privacy_findings)} finding(s) — run scanner/run.sh --check")
    refusals += [f"UNKNOWN INTEGRATION: {p['message']}" for p in result.integration_problems]
    if refusals:
        raise ExportError("refusing to export:\n  " + "\n  ".join(refusals))
    for line in result.drift_lines():
        print(f"DRIFT: {line}", file=sys.stderr)   # broken annotations export as they import: flagged, not fatal
    try:
        catalog_import.check_document(result.catalog)
    except BadRequest as exc:
        raise ExportError(f"the import would refuse it: {exc}") from exc
    return result.catalog


def main(argv: list[str] | None = None) -> int:
    from scanner import scan as scanmod

    p = argparse.ArgumentParser(prog="python -m app.catalog_export",
                                description="Build exports/catalog.json for one ERP ref, offline (#402).")
    p.add_argument("--project", default=roles.slug("export"), help="dashboard project (default %(default)s)")
    p.add_argument("--ref", "--erp-ref", dest="ref", help="git ref or sha to scan")
    p.add_argument("--repo", help="the project's repo (default: the scanner's, scanner/scan.py PROJECT_REPOS)")
    p.add_argument("--erp-tree", help="scan this already-exported tree instead of a ref")
    p.add_argument("--commit", help="with --erp-tree: the commit it was exported from")
    p.add_argument("--annotations", help="default: $CREWBOSS_HOME/scanner/annotations.yaml")
    p.add_argument("--linkedin-repo", default=scanmod.DEFAULT_LINKEDIN_REPO, help="'' to skip")
    p.add_argument("--knowledge-repo", default=scanmod.DEFAULT_KNOWLEDGE_REPO, help="'' to skip")
    p.add_argument("--out", required=True, help="file to write")
    args = p.parse_args(argv)
    try:
        catalog = scan_offline(args.project, ref=args.ref, repo=args.repo, tree=args.erp_tree, commit=args.commit,
                               annotations=args.annotations, linkedin_repo=args.linkedin_repo,
                               knowledge_repo=args.knowledge_repo)
    except ExportError as exc:
        print(f"catalog export: {exc}", file=sys.stderr)
        return exc.code
    out = Path(args.out)
    text = render(catalog)
    tmp = out.with_name(out.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, out)
    except OSError as exc:
        print(f"catalog export: couldn't write {out}: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {out}: {len(catalog['workflows'])} workflows @ {catalog['commit'][:12]}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # so `scanner.*` imports from the checkout
    sys.exit(main())
