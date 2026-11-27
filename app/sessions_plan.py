"""CLI: python -m app.sessions_plan

Prints the standing-session plan as JSON — exactly what GET /api/sessions/plan returns (repo.session_plan):
[{project, short_name, role, stream, session_name, kind}] for every ACTIVE node that runs sessions. No secrets.

Why a CLI (task #368): the plan spans every account, so GET /api/sessions/plan is the overseer's only and no longer
answers a tokenless call. The launcher (sessions/crewboss-sessions.sh) starts every account's sessions BEFORE any of them
holds a token, so it reads the plan here instead, inside the dashboard container, the same way it already mints
tokens: `docker compose exec -T app python -m app.sessions_plan`. Reaching it needs docker on the sessions server (the
launcher's own user), never the network. A failure (database down, container not running) exits non-zero with
nothing on stdout, so the launcher keeps its "unreachable → fallback" behaviour."""
from __future__ import annotations

import json
import sys

from app import repo


def main(argv: list[str] | None = None) -> int:
    try:
        plan = repo.session_plan()
    except Exception as exc:  # noqa: BLE001  (any failure = "unreachable" to the launcher)
        print(f"sessions plan unavailable: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
