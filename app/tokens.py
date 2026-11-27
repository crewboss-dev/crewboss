"""CLI: python -m app.tokens create --project acme-app --stream bugs [--name acme-bugs] [--quiet]
       python -m app.tokens create --project acme-dashboard --stream all --quiet   # the one 'all' session
       python -m app.tokens create --project acme-app --stream bugs --agent gemini --quiet   # a Gemini CLI session
       python -m app.tokens create --kind scanner --name auto-rescan --quiet > <token file>
       python -m app.tokens create --kind satellite --project acme-retail --quiet > <token file>   # a client's mini dashboard
       python -m app.tokens create --kind reporter --project acme-app [--source host-a] --quiet > <token file>   # #404
       python -m app.tokens create --kind digest --quiet > <token file>   # #828: read-only, GET /api/summary only

Prints a new bearer token ONCE (prefix cb_). The database stores only its hash — there is no way to recover a lost
token; issue a new one and revoke the old row if needed. A SESSION token (a stream, or 'all') is bound to
one project (--project, default the legacy-token project, CREWBOSS_LEGACY_TOKEN_PROJECT) and can only ever read or write that project.
`--kind` is an alias of `--stream`; machine roles (scanner) are project-less. `--quiet` prints the bare token
and nothing else, so it can be redirected straight into a 600 file without ever landing on a terminal."""
from __future__ import annotations

import argparse
import sys

from app import repo, roles
from app.auth import issue_token
from app.config import AGENTS, DEFAULT_AGENT, PROJECT_BOUND_ROLES, SESSION_KINDS, TOKEN_KINDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tokens")
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="issue a new session (or role) token")
    create.add_argument("--stream", "--kind", dest="stream", required=True, choices=TOKEN_KINDS)
    create.add_argument("--project", default=None,
                        help=f"project slug a session token is bound to (default {roles.slug('legacy_token')})")
    create.add_argument("--name", default=None, help="human label, e.g. 'erp-bugs tmux session'")
    create.add_argument("--agent", default=DEFAULT_AGENT, choices=AGENTS,
                        help=f"which AI holds the token — attribution only, same reach either way (default {DEFAULT_AGENT})")
    create.add_argument("--source", action="append", default=None,
                        help="a 'reporter' token only: a report source it may post for (repeatable; default any)")
    create.add_argument("--quiet", action="store_true", help="print only the token")

    args = parser.parse_args(argv)
    if args.cmd == "create":
        project = None
        if args.stream in SESSION_KINDS:
            try:
                project = repo.get_project_by_slug(args.project or roles.slug("legacy_token"))
            except repo.NotFound:
                print(f"no project {args.project!r}", file=sys.stderr)
                return 2
        elif args.stream in PROJECT_BOUND_ROLES:
            if not args.project:
                print(f"a {args.stream!r} token is bound to one project; give --project", file=sys.stderr)
                return 2
            try:
                project = repo.get_project_by_slug(args.project)
            except repo.NotFound:
                print(f"no project {args.project!r}", file=sys.stderr)
                return 2
        elif args.project:
            print(f"a {args.stream!r} token is not bound to a project; drop --project", file=sys.stderr)
            return 2
        if args.stream in PROJECT_BOUND_ROLES:
            name = args.name or f"{project['short_name']}-{args.stream}"
        else:
            name = args.name or (repo.session_name(project, args.stream) if project else f"crewboss-{args.stream}")
        if args.source and args.stream != "reporter":
            print("--source is for a 'reporter' token only", file=sys.stderr)
            return 2
        token = issue_token(args.stream, name, project_id=project["id"] if project else None, agent=args.agent,
                            sources=args.source)
        if not args.quiet:
            where = f" project={project['slug']!r}" if project else ""
            print(f"Token for stream={args.stream!r}{where} agent={args.agent!r} name={name!r} "
                  "(shown once — store it now):")
        print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
