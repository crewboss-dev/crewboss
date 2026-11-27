#!/usr/bin/env python3
"""Keeps the dashboard's workflow catalog current: run every 5 min by a timer (scanner/systemd/*.example).

Settings (repos, project, import URL, token file, state folder) come from $CREWBOSS_HOME/scanner/scanner.yaml and the annotations
from $CREWBOSS_HOME/scanner/annotations.yaml (scanner/config.py).

    python -m scanner.auto_rescan              # fetch, rescan + import only if something changed
    python -m scanner.auto_rescan --force      # rescan + import even if nothing changed
    python -m scanner.auto_rescan --dry-run    # fetch + scan + validate, but don't POST or save state

Each run:
1. `git fetch origin` in the primary, secondary and knowledge repos (the last holds the Infrastructure annotations'
   code: infra/ host automations) — updates their remote-tracking refs only; never checks out, resets or otherwise
   touches a working tree.
2. Fingerprints the inputs: each repo's origin/main SHA, the annotations file, and the scanner's
   own code. If that equals the fingerprint of the last SUCCESSFUL import (state_dir/rescan.json) it stops there — unchanged. So an annotation fix (or a scanner fix)
   shows up on the next tick even when neither repo moved.
3. Otherwise exports each origin/main via `git archive` into a temp dir (scan.export_repo — the
   scanner's existing approach), scans (scan.scan_trees), and REFUSES to import a catalog with any
   schema problem or privacy finding.
4. POSTs it to the dashboard's /api/catalog/import with the `scanner` role token read from
   the configured token file (never printed, never logged).
5. Records the new fingerprint only after the import succeeded — a failed run is retried next tick.

Any failure is logged (→ journald) and exits 1 so `systemctl status` / `systemctl --failed` show it.
Every run — success, unchanged or failure — rewrites a small status JSON
(state_dir/rescan-status.json) for the Health page / a Zabbix item to read.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # so `scanner.*` imports work run either way

from scanner import config as scanner_config  # noqa: E402
from scanner import scan as scanmod  # noqa: E402

log = logging.getLogger("crewboss-rescan")

SCANNER_DIR = Path(__file__).resolve().parent
GIT_TIMEOUT = 180
HTTP_TIMEOUT = 120


class RescanError(Exception):
    """A failure worth a non-zero exit. The message is logged and lands in the status file."""


# --- small helpers ----------------------------------------------------------------------------------

def _now() -> tuple[str, int]:
    t = time.time()
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="seconds"), int(t)


def _write_json_atomic(path: Path, data: dict, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _git(repo: str, *args: str) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_SSH_COMMAND=os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes"))
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env,
                           timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RescanError(f"git {' '.join(args)} in {repo} timed out after {GIT_TIMEOUT}s") from exc
    if r.returncode != 0:
        raise RescanError(f"git {' '.join(args)} in {repo} failed: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def _fetch(repo: str, attempts: int = 2) -> None:
    """`git fetch origin` (remote-tracking refs only). One retry: a peer's concurrent fetch in the same
    checkout can briefly hold a ref lock."""
    for i in range(attempts):
        try:
            _git(repo, "fetch", "--quiet", "origin")
            return
        except RescanError:
            if i == attempts - 1:
                raise
            time.sleep(5)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _scanner_code_fingerprint() -> str:
    """The scanner's own source (not its tests): a scanner fix re-imports on the next tick too."""
    h = hashlib.sha256()
    for p in sorted(SCANNER_DIR.rglob("*.py")):
        rel = p.relative_to(SCANNER_DIR)
        if rel.parts[0] in ("tests", "out") or "__pycache__" in rel.parts:
            continue
        h.update(str(rel).encode())
        h.update(b"\x00")
        h.update(p.read_bytes())
    return h.hexdigest()


def read_token(path: Path) -> str:
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise RescanError(f"no scanner token at {path} (issue one: see scanner/README.md, "
                          f"'Auto-rescan')") from exc
    if st.st_mode & 0o077:
        log.warning("token file %s is readable by group/other — chmod 600 it", path)
    token = path.read_text().strip()
    if not token:
        raise RescanError(f"scanner token file {path} is empty")
    return token


def post_catalog(url: str, project: str, rendered: str, token: str) -> dict:
    full = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode({'project': project})}"
    req = urllib.request.Request(full, data=rendered.encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "User-Agent": "crewboss-rescan"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300].replace(token, "***")
        raise RescanError(f"import rejected: HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RescanError(f"import failed: can't reach {url}: {getattr(exc, 'reason', exc)}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise RescanError(f"import returned non-JSON from {url}: {body[:200]!r}") from exc


# --- the run ----------------------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--primary-repo", default=scanmod.DEFAULT_ERP_REPO)
    p.add_argument("--secondary-repo", default=scanmod.DEFAULT_LINKEDIN_REPO,
                   help="pass '' to scan without the companion app")
    p.add_argument("--knowledge-repo", default=scanmod.DEFAULT_KNOWLEDGE_REPO,
                   help="pass '' to scan without the host-automation repo (Infrastructure refs then go unchecked)")
    p.add_argument("--ref", default=scanmod.DEFAULT_REF)
    p.add_argument("--annotations", default=scanner_config.annotations_path())
    p.add_argument("--url", default=scanner_config.import_url())
    p.add_argument("--project", default=scanner_config.project())
    p.add_argument("--token-file", default=scanner_config.token_file())
    p.add_argument("--state-dir", default=scanner_config.state_dir())
    p.add_argument("--force", action="store_true", help="rescan + import even if nothing changed")
    p.add_argument("--dry-run", action="store_true", help="scan + validate only: no POST, no state change")
    p.add_argument("--no-fetch", action="store_true", help="skip `git fetch` (tests / offline)")
    return p.parse_args(argv)


def _repos(args) -> dict[str, str]:
    if not args.primary_repo:
        raise RescanError("no primary repo: set repos.primary in $CREWBOSS_HOME/scanner/scanner.yaml (or pass --primary-repo)")
    # keyed by role; the catalog's `commits` map and the fingerprint use each folder's name (scanner/config.py labels())
    repos = {"primary": args.primary_repo}
    if args.secondary_repo:
        repos["secondary"] = args.secondary_repo
    if args.knowledge_repo:
        repos["knowledge"] = args.knowledge_repo
    for name, path in repos.items():
        if not Path(path, ".git").exists():
            raise RescanError(f"{name}: {path} is not a git checkout")
    return repos


def run(args) -> dict:
    """One tick. Returns the status fields to record; raises RescanError on failure."""
    state_dir = Path(args.state_dir)
    state = _read_json(state_dir / "rescan.json")
    repos = _repos(args)
    if scanner_config.annotations_required() and not Path(args.annotations).is_file():
        raise RescanError(f"annotations file {args.annotations} is missing: refusing to import a catalog without it "
                          f"(scanner.yaml `annotations: optional` if this install has none)")

    # fingerprint and logs are keyed by each checkout's folder name, the same keys as the catalog's `commits` map
    labels = {role: Path(path).name for role, path in repos.items()}
    role_shas = {}
    for role, path in repos.items():
        if not args.no_fetch:
            _fetch(path)
        role_shas[role] = _git(path, "rev-parse", "--verify", f"{args.ref}^{{commit}}")
    shas = {labels[role]: sha for role, sha in role_shas.items()}
    inputs = {"commits": shas, "annotations": _sha256_file(Path(args.annotations)),
              "scanner": _scanner_code_fingerprint()}

    last = state.get("last_import") or {}
    if not args.force and last.get("inputs") == inputs:
        log.info("unchanged since scan #%s (%s) — nothing to do", last.get("scan_id"),
                 ", ".join(f"{k}@{v[:12]}" for k, v in shas.items()))
        return {"status": "unchanged", "commits": shas, "scan_id": last.get("scan_id")}

    why = "forced" if args.force else ", ".join(
        k for k in ("commits", "annotations", "scanner") if (last.get("inputs") or {}).get(k) != inputs[k]
    ) or "first run"
    log.info("rescanning (%s changed): %s", why, ", ".join(f"{k}@{v[:12]}" for k, v in shas.items()))

    cleanup: list[Path] = []
    try:
        erp_root, erp_sha = scanmod.export_repo(repos["primary"], role_shas["primary"], fetch=False)
        cleanup.append(erp_root)
        linkedin_root = linkedin_sha = None
        if "secondary" in repos:
            linkedin_root, linkedin_sha = scanmod.export_repo(repos["secondary"], role_shas["secondary"], fetch=False)
            cleanup.append(linkedin_root)
        knowledge_root = knowledge_sha = None
        if "knowledge" in repos:
            knowledge_root, knowledge_sha = scanmod.export_repo(repos["knowledge"], role_shas["knowledge"], fetch=False)
            cleanup.append(knowledge_root)
        result = scanmod.scan_trees(erp_root, linkedin_root, erp_sha, linkedin_sha, args.annotations,
                                    knowledge_root=knowledge_root, knowledge_sha=knowledge_sha,
                                    project=args.project, labels=labels)
    except subprocess.CalledProcessError as exc:
        raise RescanError(f"git archive/tar failed: {exc}") from exc
    finally:
        for p in cleanup:
            shutil.rmtree(p, ignore_errors=True)

    log.info("%s", result.summary())
    for line in result.drift_lines():
        log.warning("DRIFT: %s", line)
    if not result.ok:
        for p in result.problems[:20]:
            log.error("SCHEMA: %s", p)
        # privacy findings quote what they matched — log only the count, never the matched text
        raise RescanError(f"refusing to import: {len(result.problems)} schema problem(s), "
                          f"{len(result.privacy_findings)} privacy finding(s) — run "
                          f"`scanner/run.sh --check` for details")

    fields = {"commits": shas, "workflows": len(result.catalog["workflows"]),
              "broken": len(result.broken)}
    if args.dry_run:
        log.info("dry run: not importing")
        return dict(fields, status="dry-run")

    token = read_token(Path(args.token_file))
    try:
        imported = post_catalog(args.url, args.project, result.rendered, token)
    finally:
        del token
    log.info("imported scan #%s: %s workflows, %s changed, %s newly stale, %s broken",
             imported.get("scan_id"), imported.get("workflow_count"), imported.get("diff_count"),
             imported.get("stale_count"), imported.get("broken_count"))

    at, _ = _now()
    _write_json_atomic(state_dir / "rescan.json", {
        "last_import": {"inputs": inputs, "at": at, "scan_id": imported.get("scan_id")}}, mode=0o600)
    _write_json_atomic(state_dir / "last-catalog.json", result.catalog, mode=0o600)
    return dict(fields, status="imported", scan_id=imported.get("scan_id"),
                diff_count=imported.get("diff_count"), stale=imported.get("stale_count"))


def write_status(state_dir: Path, fields: dict, error: str | None) -> None:
    """The status file a Health page / Zabbix item reads. `ok` is the single boolean to alert on;
    `last_success_epoch` going stale (> ~15 min) means the timer itself stopped firing."""
    path = state_dir / "rescan-status.json"
    prev = _read_json(path)
    at, epoch = _now()
    status = {
        "ok": error is None,
        "status": "failed" if error else fields.get("status"),
        "last_run_at": at, "last_run_epoch": epoch,
        "last_error": error,
        "consecutive_failures": (prev.get("consecutive_failures", 0) + 1) if error else 0,
        "last_success_at": prev.get("last_success_at"), "last_success_epoch": prev.get("last_success_epoch"),
        "last_import_at": prev.get("last_import_at"), "last_import_epoch": prev.get("last_import_epoch"),
        "scan_id": prev.get("scan_id"), "commits": prev.get("commits"),
        "workflows": prev.get("workflows"), "broken": prev.get("broken"),
    }
    if error is None:
        status.update(last_success_at=at, last_success_epoch=epoch)
        for k in ("commits", "scan_id", "workflows", "broken"):
            if fields.get(k) is not None:
                status[k] = fields[k]
        if fields.get("status") == "imported":
            status.update(last_import_at=at, last_import_epoch=epoch)
    _write_json_atomic(path, status)


@contextlib.contextmanager
def _lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "rescan.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    args = parse_args(argv)
    state_dir = Path(args.state_dir)
    with _lock(state_dir) as got:
        if not got:
            log.info("another rescan is already running — skipping this tick")
            return 0
        try:
            fields = run(args)
        except RescanError as exc:
            log.error("rescan failed: %s", exc)
            if not args.dry_run:
                write_status(state_dir, {}, str(exc))
            return 1
        except Exception as exc:  # noqa: BLE001 — anything unexpected is still a visible failure
            log.exception("rescan crashed")
            if not args.dry_run:
                write_status(state_dir, {}, f"crashed: {type(exc).__name__}: {exc}")
            return 1
        if not args.dry_run:
            write_status(state_dir, fields, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
