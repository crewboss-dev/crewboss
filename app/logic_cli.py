"""Console commands for the logic map (task #715): `python -m app.cli logic ...` (or `crewboss logic ...` from a checkout).

    logic check [--dir .] [--strict]    schema errors, stale references and uncovered entry points; exit 1 on errors or stale (--strict: also uncovered)
    logic uncovered [--dir .]           the entry points in the code that no workflow names
    logic report [--dir .] [--sha SHA] [--post URL --project SLUG --token-file F]
                                        the report the release gate stores (JSON on stdout, or posted to /api/logic/report)
    logic import-catalog CATALOG.json [--dir .] [--name F]   one-off: today's scanner catalog -> .crewboss/logic/F
    logic export [--dir .] [--format json|html] [-o FILE]
                                        the slim map, for an app's own admin page (JSON), or one self-contained HTML page

Pure: needs only PyYAML and the checkout; no database, no network except `report --post`."""
from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

if "crewboss_logic_map" in sys.modules:         # loaded by scripts/crewboss_logic.py, which must not import the repo's own `app` package
    logic_map = sys.modules["crewboss_logic_map"]
else:
    from app import logic_map


def _git_sha(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def cmd_check(args) -> int:
    c = logic_map.check(args.dir)
    m = c["map"]
    print(f"{len(m['workflows'])} workflows in {len(m['files'])} file(s); {c['discovered']} entry points found in the code, "
          f"{len(c['uncovered'])} uncovered, {c['ignored']} ignored on purpose.")
    for e in m["errors"]:
        print(f"ERROR   {e}")
    for s in c["stale"]:
        print(f"STALE   {s['workflow']} / {s['step']}: {s['why']}")
    for u in c["uncovered"][:args.limit]:
        print(f"UNCOVERED {u['kind']:8} {u['name']}  ({u['file']}:{u['line']}, {u['function'] or '-'})")
    if len(c["uncovered"]) > args.limit:
        print(f"... and {len(c['uncovered']) - args.limit} more (logic uncovered lists them all)")
    bad = bool(m["errors"] or c["stale"] or (args.strict and c["uncovered"]))
    print("FAILED" if bad else "OK")
    return 1 if bad else 0


def cmd_uncovered(args) -> int:
    for u in logic_map.check(args.dir)["uncovered"]:
        print(f"{u['kind']:8} {u['name']}\t{u['file']}:{u['line']}\t{u['function'] or '-'}")
    return 0


def cmd_report(args) -> int:
    root = Path(args.dir)
    sha = (args.sha or _git_sha(root)).strip().lower()
    report = logic_map.build_report(root, sha)
    if not args.post:
        json.dump(report, sys.stdout, indent=2)
        print()
        return 0
    if not args.project or not args.token_file:
        raise SystemExit("--post needs --project and --token-file")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    url = f"{args.post.rstrip('/')}/api/logic/report?sha={sha}&project={args.project}"
    req = urllib.request.Request(url, data=json.dumps(report).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"the dashboard refused the report: {exc.code} {exc.read().decode()[:300]}") from None
    return 0


def render_html(workflows: list[dict]) -> str:
    """One self-contained page (no scripts): every workflow with its trigger, steps, data and side effects."""
    out = ["<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Logic map</title>",
           "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:56rem;margin:1rem auto;padding:0 1rem;color:#222}"
           "h2{margin:1.6rem 0 .2rem}.t{color:#555}li{margin:.25rem 0}code{background:#0000000d;padding:0 .25rem;border-radius:3px}"
           ".ai{color:#5b3fa8}</style><h1>Logic map</h1>"]
    for w in workflows:
        t = w["trigger"]
        out.append(f"<section id='{html.escape(w['id'])}'><h2>{html.escape(w['name'])}</h2><p>{html.escape(w['summary'])}</p>")
        start = ' · '.join(x for x in (t.get("type"), t.get("control"), t.get("detail")) if x)
        out.append(f"<p class=t>Starts: {html.escape(start)}</p><ol>")
        for s in w["steps"]:
            extra = ""
            if s.get("question"):
                extra += f" <em>{html.escape(s['question'])}</em>"
            if s.get("ai"):
                a = s["ai"]
                extra += f" <span class=ai>AI: {html.escape(a.get('skill') or a.get('prompt') or '')} {html.escape(a.get('model') or '')}</span>"
            if s.get("effects"):
                extra += " <code>" + html.escape(", ".join(s["effects"])) + "</code>"
            out.append(f"<li><strong>{html.escape(s['label'])}</strong>{extra}</li>")
        out.append("</ol></section>")
    return "".join(out)


def cmd_export(args) -> int:
    m = logic_map.load(args.dir)
    if m["errors"]:
        for e in m["errors"]:
            print(f"ERROR   {e}", file=sys.stderr)
        raise SystemExit("the map has errors: fix them first (logic check)")
    slim = logic_map.slim(m["workflows"])
    text = render_html(m["workflows"]) if args.format == "html" else json.dumps({"version": 1, "workflows": slim}, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output} ({len(slim)} workflows)")
    else:
        sys.stdout.write(text)
    return 0


def cmd_import_catalog(args) -> int:
    """One-off: today's scanner catalog (JSON) -> .crewboss/logic/<name>.yaml, checked before it is reported."""
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    workflows = logic_map.from_catalog(catalog)
    if not workflows:
        raise SystemExit("the catalog has no workflows to convert")
    out = Path(args.dir) / logic_map.LOGIC_DIR / args.name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(logic_map.dump_workflows(workflows), encoding="utf-8")
    m = logic_map.load(args.dir)
    print(f"wrote {out} ({len(workflows)} workflows)")
    for e in m["errors"]:
        print(f"ERROR   {e}", file=sys.stderr)
    return 1 if m["errors"] else 0


def add_parser(sub) -> None:
    lg = sub.add_parser("logic", help="the logic map: check, uncovered, report, export")
    s = lg.add_subparsers(dest="action", required=True)

    def common(p):
        p.add_argument("--dir", default=".", help="the app's checkout (default: here)")

    c = s.add_parser("check", help="schema errors, stale references, uncovered entry points")
    common(c)
    c.add_argument("--strict", action="store_true", help="uncovered entry points fail too")
    c.add_argument("--limit", type=int, default=25)
    c.set_defaults(func=cmd_check)
    u = s.add_parser("uncovered", help="entry points no workflow names")
    common(u)
    u.set_defaults(func=cmd_uncovered)
    r = s.add_parser("report", help="the report the release gate stores")
    common(r)
    r.add_argument("--sha", default="", help="the commit (default: git HEAD of --dir)")
    r.add_argument("--post", metavar="URL", help="the dashboard's base URL: post the report to it")
    r.add_argument("--project", default="", help="with --post: the project's slug")
    r.add_argument("--token-file", default="", help="with --post: a file holding a scanner or session token")
    r.set_defaults(func=cmd_report)
    i = s.add_parser("import-catalog", help="one-off: convert a scanner catalog JSON into .crewboss/logic/")
    common(i)
    i.add_argument("catalog", help="the scanner output (docs/catalog-schema.md)")
    i.add_argument("--name", default="catalog.yaml", help="the file to write under .crewboss/logic/")
    i.set_defaults(func=cmd_import_catalog)
    e = s.add_parser("export", help="the slim map for an app's own admin page")
    common(e)
    e.add_argument("--format", choices=("json", "html"), default="json")
    e.add_argument("-o", "--output", default="")
    e.set_defaults(func=cmd_export)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli logic")
    sub = parser.add_subparsers(dest="group", required=True)
    add_parser(sub)
    args = parser.parse_args(["logic", *(argv if argv is not None else sys.argv[1:])])
    return args.func(args)
