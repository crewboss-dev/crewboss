#!/usr/bin/env python3
"""Entry point: scans the configured repos and writes the workflow catalog (docs/catalog-schema.md is the
contract). Which repos, and the annotations, come from the instance folder (scanner/config.py). Run via `./run.sh`, or directly:

    python -m scanner.scan                       # scan + write out/catalog-<sha>.json + out/latest.json
    python -m scanner.scan --check                # scan, validate + privacy-sweep, exit 1 on any problem
    python -m scanner.scan --primary-root /some/checkout --no-fetch   # scan an existing checkout in place
    python -m scanner.scan --diff out/latest.json  # scan, then print what changed vs an older catalog

Repo handling (per the brief: "don't disturb the working tree peers use"): by default this NEVER
touches the primary repo's working tree. It runs `git fetch` there, then `git archive <ref> | tar -x`
into a throwaway temp directory and scans THAT — the same trick used to build this scanner in the
first place. Pass --primary-root to point at an already-exported tree instead (e.g. in CI, or the temp dir
this script already extracted, for a second run without re-fetching).
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # so `scanner.*` imports work run either way

from app import instance
from scanner import annotations as ann_loader
from scanner import config as scanner_config
from scanner.build import AUTO_DECISIONS_DEFAULT, build_catalog
from scanner.lib import diff as difflib
from scanner.lib import integrations as integrations_lib
from scanner.lib import schema

# The repos come from $CREWBOSS_HOME/scanner/scanner.yaml (scanner/config.py): primary = the application, secondary = a
# companion app, knowledge = the host-automation repo (annotations with subproject Infrastructure point into its infra/).
DEFAULT_ERP_REPO = scanner_config.repo_path("primary")
DEFAULT_LINKEDIN_REPO = scanner_config.repo_path("secondary")
DEFAULT_KNOWLEDGE_REPO = scanner_config.repo_path("knowledge")
DEFAULT_REF = scanner_config.ref()
# Which repo each dashboard project's catalog is built from. The scanner's discovery (build.py) knows one application's code
# shape only, so that project is the one it can build; app/catalog_export.py's CLI reads its repo from here, and
# drift.yaml's `repo` for the project must match (tests/test_drift.py holds them together).
PROJECT_REPOS = dict(instance.get_map("project_repos"))


def _run(cmd: list[str], cwd: str | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def export_repo(repo_dir: str, ref: str, fetch: bool) -> tuple[Path, str]:
    """git fetch (optional) + git archive <ref> into a fresh temp dir. Returns (path, resolved sha).
    Never runs inside `repo_dir` beyond read-only `git fetch`/`git archive`/`git rev-parse` — no
    checkout, no reset, nothing that could disturb a peer's working tree."""
    if fetch:
        _run(["git", "fetch", "origin"], cwd=repo_dir)
    sha = _run(["git", "rev-parse", ref], cwd=repo_dir)
    tmp = Path(tempfile.mkdtemp(prefix="dash-scan-"))
    archive = subprocess.run(["git", "archive", sha], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["tar", "-x"], input=archive.stdout, cwd=tmp, check=True)
    return tmp, sha


@dataclass
class ScanResult:
    catalog: dict
    rendered: str
    problems: list = field(default_factory=list)          # schema problems (hard failure under --check)
    privacy_findings: list = field(default_factory=list)  # privacy sweep hits (hard failure under --check)

    @property
    def broken(self) -> list[dict]:
        """Annotated workflows whose references no longer resolve (scanner/lib/drift.py). NOT a scan
        failure: the catalog carries them as `problems` so the app flags them + raises a Tweak task."""
        return [w for w in self.catalog["workflows"] if w.get("problems")]

    @property
    def ok(self) -> bool:
        return not self.problems and not self.privacy_findings

    def uncovered_lines(self) -> list[str]:
        """One line per business table no workflow covers (scanner/lib/coverage.py) — a report, not a failure."""
        out = []
        for e in (self.catalog.get("coverage") or {}).get("uncovered") or []:
            ws = [f"{w['file']}::{w['function']}" for w in e["writers"]]
            out.append(f"{e['table']} <- " + ", ".join(ws[:4]) + (f" (+{len(ws) - 4} more)" if len(ws) > 4 else ""))
        return out

    @property
    def integration_problems(self) -> list[dict]:
        """Annotations naming an integration id the ERP's export doesn't know (scanner/lib/integrations.py): drift,
        so the import still happens (the workflow shows broken), but `--check` fails on it."""
        return [p for w in self.broken for p in w["problems"] if p.get("code") == integrations_lib.UNKNOWN_INTEGRATION]

    def drift_lines(self) -> list[str]:
        return [f"{w['id']}: {p['message']}" for w in self.broken for p in w["problems"]]

    def summary(self) -> str:
        wfs = self.catalog["workflows"]
        n_ai = sum(1 for w in wfs if w["has_ai"])
        refs = [r for w in wfs for r in w.get("source_refs") or []]
        n_hashed = sum(1 for r in refs if r.get("hash"))
        n_dec = sum(1 for w in wfs if any(s.get("kind") == "decision" for s in w.get("steps") or []))
        n_loop = sum(1 for w in wfs if any(s.get("kind") == "loop" for s in w.get("steps") or []))
        cov = self.catalog.get("coverage") or {}
        return (f"{len(wfs)} workflow(s), {n_ai} with AI, {n_dec} with decisions, {n_loop} with loops, "
                f"{len(self.problems)} schema problem(s), "
                f"{len(self.privacy_findings)} privacy finding(s), {len(self.broken)} broken annotation(s), "
                f"{n_hashed}/{len(refs)} source_refs hashed, "
                f"{cov.get('covered', 0)}/{cov.get('business_tables', 0)} written business tables covered, "
                + (f"{len(self.catalog['integrations'].get('integrations') or [])} integrations from the ERP"
                   if isinstance(self.catalog.get("integrations"), dict) else "no integrations list on the ERP's main"))


def scan_trees(erp_root: Path, linkedin_root: Path | None, erp_sha: str, linkedin_sha: str | None,
               annotations_path: str | Path, auto_decisions: bool | None = None,
               knowledge_root: Path | None = None, knowledge_sha: str | None = None,
               project: str | None = None, labels: dict | None = None) -> ScanResult:
    """Discover + merge + hash + drift-check + validate + privacy-sweep already-exported trees.
    Pure (writes nothing) — shared by main() and scanner/auto_rescan.py."""
    annotations = ann_loader.load(annotations_path)
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if auto_decisions is None:
        auto_decisions = AUTO_DECISIONS_DEFAULT
    catalog = build_catalog(erp_root, linkedin_root, annotations, erp_sha, linkedin_sha, generated_at,
                            auto_decisions=auto_decisions, knowledge_root=knowledge_root,
                            knowledge_sha=knowledge_sha,
                            project=project if project is not None else scanner_config.project(),
                            labels=labels if labels is not None else scanner_config.labels())
    # the ERP's graded integration list rides along (#393), and annotations naming an unknown id become drift
    integ, integ_problems = integrations_lib.load(erp_root)
    if integ is not None:
        catalog["integrations"] = integ
        integrations_lib.attach(catalog["workflows"], integ)
    rendered = json.dumps(catalog, indent=2, sort_keys=False)
    return ScanResult(catalog=catalog, rendered=rendered, problems=integ_problems + schema.validate_catalog(catalog),
                      privacy_findings=schema.privacy_sweep(rendered))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--primary-repo", default=DEFAULT_ERP_REPO, help="path to the primary application's checkout (default: scanner.yaml repos.primary)")
    p.add_argument("--secondary-repo", default=DEFAULT_LINKEDIN_REPO,
                   help="path to the companion app's checkout (skipped if missing)")
    p.add_argument("--primary-root", default=None, help="scan an already-exported primary tree instead of archiving")
    p.add_argument("--secondary-root", default=None, help="scan an already-exported companion tree instead")
    p.add_argument("--knowledge-repo", default=DEFAULT_KNOWLEDGE_REPO,
                   help="path to the host-automation repo's checkout (Infrastructure annotations; skipped if missing)")
    p.add_argument("--knowledge-root", default=None, help="scan an already-exported host-automation tree instead")
    p.add_argument("--ref", default=DEFAULT_REF, help="git ref to archive (default origin/main)")
    p.add_argument("--no-fetch", action="store_true", help="skip `git fetch` before archiving")
    p.add_argument("--annotations", default=scanner_config.annotations_path(),
                   help="default: $CREWBOSS_HOME/scanner/annotations.yaml")
    p.add_argument("--out-dir", default=str(Path(__file__).parent / "out"))
    p.add_argument("--check", action="store_true",
                   help="scan, then validate against the schema + run the privacy sweep; "
                        "exit 1 (and skip writing output) on ANY problem")
    p.add_argument("--auto-decisions", dest="auto_decisions", action="store_true", default=None,
                   help="derive decision cards for AUTO workflows from if/elif/else and match statements "
                        "that choose between different side-effect steps (the default — see README)")
    p.add_argument("--no-auto-decisions", dest="auto_decisions", action="store_false",
                   help="leave every auto workflow a straight run")
    p.add_argument("--diff", metavar="OLD_CATALOG_JSON", default=None,
                   help="scan, then print what changed per workflow id vs an older catalog file "
                        "(added/removed workflows; added/removed/changed steps; prompt_text/"
                        "model_rule/max_tokens changes on 'ai' steps) — does not affect exit status")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cleanup: list[Path] = []
    try:
        if args.primary_root:
            erp_root = Path(args.primary_root)
            erp_sha = _run(["git", "rev-parse", "HEAD"], cwd=args.primary_root) if (erp_root / ".git").exists() else "unknown"
        else:
            if not args.primary_repo:
                print("no primary repo: set repos.primary in $CREWBOSS_HOME/scanner/scanner.yaml, or pass --primary-repo",
                      file=sys.stderr)
                return 2
            erp_root, erp_sha = export_repo(args.primary_repo, args.ref, fetch=not args.no_fetch)
            cleanup.append(erp_root)

        linkedin_root = None
        linkedin_sha = None
        if args.secondary_root:
            linkedin_root = Path(args.secondary_root)
            linkedin_sha = "unknown"
        elif args.secondary_repo and Path(args.secondary_repo).is_dir():
            try:
                linkedin_root, linkedin_sha = export_repo(args.secondary_repo, args.ref, fetch=not args.no_fetch)
                cleanup.append(linkedin_root)
            except subprocess.CalledProcessError:
                print(f"warning: could not export {args.secondary_repo} — skipping the companion app's discovery",
                      file=sys.stderr)

        knowledge_root = None
        knowledge_sha = None
        if args.knowledge_root:
            knowledge_root = Path(args.knowledge_root)
            knowledge_sha = "unknown"
        elif args.knowledge_repo and Path(args.knowledge_repo).is_dir():
            try:
                knowledge_root, knowledge_sha = export_repo(args.knowledge_repo, args.ref,
                                                            fetch=not args.no_fetch)
                cleanup.append(knowledge_root)
            except subprocess.CalledProcessError:
                print(f"warning: could not export {args.knowledge_repo} — Infrastructure refs not checked",
                      file=sys.stderr)

        result = scan_trees(erp_root, linkedin_root, erp_sha, linkedin_sha, args.annotations,
                            auto_decisions=args.auto_decisions, knowledge_root=knowledge_root,
                            knowledge_sha=knowledge_sha)
        catalog, rendered = result.catalog, result.rendered
        problems, privacy_findings = result.problems, result.privacy_findings

        for p in problems:
            print(f"SCHEMA: {p}", file=sys.stderr)
        for f in privacy_findings:
            print(f"PRIVACY: {f}", file=sys.stderr)
        for line in result.drift_lines():
            print(f"DRIFT: {line}", file=sys.stderr)
        for line in result.uncovered_lines():
            print(f"UNCOVERED: {line}", file=sys.stderr)

        print(result.summary())

        if args.diff:
            old = json.loads(Path(args.diff).read_text())
            d = difflib.diff_catalogs(old, catalog)
            print()
            print(difflib.render_report(d, old_commit=old.get("commit", ""), new_commit=erp_sha))

        if problems or privacy_findings:
            if args.check:
                return 1
            print("writing output despite the above (run with --check to make this a hard failure)",
                  file=sys.stderr)

        if args.check:
            if result.integration_problems:
                print(f"{len(result.integration_problems)} annotation(s) name an integration the ERP doesn't list",
                      file=sys.stderr)
                return 1
            return 0  # a check validates only: it never replaces out/latest.json (README, run.sh)

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        dated = out_dir / f"catalog-{erp_sha[:12]}.json"
        dated.write_text(rendered)
        (out_dir / "latest.json").write_text(rendered)
        print(f"wrote {dated}")
        print(f"wrote {out_dir / 'latest.json'}")
        return 0
    finally:
        for p in cleanup:
            shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
