"""Console commands for the app's own sign-in (task #706; password + authenticator app by default since #976).

    python -m app.cli owner create [--username owner] [--reset]
    python -m app.cli reset-owner
    python -m app.cli logic check|uncovered|report|export     (the logic map, app/logic_cli.py; needs no database)

`owner create` makes the single owner of a local-sign-in install and prints a ONE-TIME TEMPORARY PASSWORD (generated here, 24 hours,
single use, shown once). On the sign-in page: the owner's username, the temporary password as the password with the code box empty; that
opens a restricted session where they choose their own password, set up an authenticator app and save their recovery codes. With an owner
already present it refuses, unless --reset.

`reset-owner` is the owner's way back when the recovery codes are gone too (an admin can't reset the owner from the page): it removes the
owner's password, passkeys, authenticator and recovery codes, ends their sessions and prints a new temporary password. It exists ONLY here, on the server's shell:
no web route can call it.

Run it where the app's DATABASE_URL is set (inside the app container: `docker compose exec app python -m app.cli reset-owner`, or
`./crewboss reset-owner`)."""
from __future__ import annotations

import argparse
import sys



def owner_create(args: argparse.Namespace) -> int:
    from app import config, local_auth
    username = local_auth.normalise_username(args.username)
    if not local_auth.valid_username(username):
        raise SystemExit("The username must be 3-64 characters: lowercase letters, digits, '.', '_' or '-'.")
    if problem := local_auth.key_problem():       # make the encryption key now, and fail loudly if it can't be
        raise SystemExit(problem)
    try:
        _uid, pw = local_auth.create_owner(username, reset=args.reset)
    except local_auth.OwnerExists:
        raise SystemExit("An owner already exists. Use --reset (or reset-owner) to wipe how they sign in and get a new temporary password.") from None
    print(f"Owner {username!r} {'reset' if args.reset else 'created'}.")
    print(f"Temporary password (valid {local_auth.TEMP_PASSWORD_HOURS} hours, works once, shown once): {pw}")
    print(f"Next: open {config.PUBLIC_URL}/auth/login, enter {username!r}, type the temporary password as the password (leave the code empty),")
    print("then choose your own password, set up an authenticator app on your phone and save the recovery codes.")
    return 0


def reset_owner(args: argparse.Namespace) -> int:
    from app import config, local_auth
    if not (args.yes or sys.stdin.isatty()):
        raise SystemExit("reset-owner runs from the server's shell (a terminal). Pass --yes to run it from a script on the server.")
    if problem := local_auth.key_problem():
        raise SystemExit(problem)
    try:
        username, pw = local_auth.reset_owner()
    except LookupError as exc:
        raise SystemExit(f"{exc}. Make one with: python -m app.cli owner create") from None
    print(f"Owner {username!r}: password, passkeys, authenticator and recovery codes removed, every session ended.")
    print(f"Temporary password (valid {local_auth.TEMP_PASSWORD_HOURS} hours, works once, shown once): {pw}")
    print(f"Next: open {config.PUBLIC_URL}/auth/login, enter {username!r}, type it as the password (leave the code empty), then choose a new password and set up the authenticator app again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="group", required=True)
    owner = sub.add_parser("owner", help="the owner account").add_subparsers(dest="action", required=True)
    create = owner.add_parser("create", help="create the owner and print a one-time temporary password")
    create.add_argument("--username", default="owner")
    create.add_argument("--reset", action="store_true", help="replace an existing owner's sign-in: passkeys, codes and sessions cleared")
    create.set_defaults(func=owner_create)
    ro = sub.add_parser("reset-owner", help="remove the owner's passkeys and recovery codes and print a new temporary password (server shell only)")
    ro.add_argument("--yes", action="store_true", help="run without a terminal (a script on the server)")
    ro.set_defaults(func=reset_owner)
    from app import logic_cli      # the logic map commands (task #715): pure, no database
    logic_cli.add_parser(sub)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
