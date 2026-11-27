"""Users and roles (task #713; #976): the owner adds people, each gets a ONE-TIME TEMPORARY PASSWORD (generated here, shown once, stored
hashed, 24 hours, single use) and signs in with it only to choose their own password, set up an authenticator app and save recovery codes.

The owner is made on the console (python -m app.cli owner create), never from the page; there is exactly one (users_one_owner). Owner-run
actions here: add a person, change role, disable / enable, "Reset sign-in" (removes their password, passkeys, authenticator and recovery codes, ends their sessions
and issues a new temporary password), sign them out everywhere, clear their sign-in email. The owner account cannot be reset from the page;
its way back is a recovery code or `python -m app.cli reset-owner` on the server."""
from __future__ import annotations

from psycopg.errors import UniqueViolation

from app import access, local_auth as la, repo
from app.db import get_cursor
from app.repo import BadRequest, Conflict, NotFound


def _log(cur, actor: str, verb: str, entity: str, entity_id: int | None, summary: str) -> None:
    """User admin is INSTANCE-level: it goes to the instance audit (only the instance owner reads it), never into a project's feed
    where a project's members could see who was added or reset."""
    access.audit(cur, verb, summary)


# --- adding a person ----------------------------------------------------------------------------------------------

def create_user(username: str, role: str, email: str = "") -> tuple[dict, str]:
    """Make a person with a server-generated temporary password. Returns (user row, the password, shown ONCE). BadRequest (shown to the
    owner) for a bad role, username or email, or a taken username or email."""
    if role not in access.INVITE_ROLES:
        raise BadRequest(f"role must be one of {', '.join(access.INVITE_ROLES)}")
    username = la.normalise_username(username)
    if not la.valid_username(username):
        raise BadRequest("Pick a username of 3-64 letters, digits, dots, dashes or underscores.")
    email = (email or "").strip()
    if email and not la.valid_email(email):
        raise BadRequest("That doesn't look like an email address.")
    try:
        with get_cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE lower(username) = %s", (username,))
            if cur.fetchone():
                raise BadRequest("That username is taken.")
            cur.execute("INSERT INTO users (oid, display_name, username, email, role) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                        ("local:" + username, username, username, email or None, role))
            user = cur.fetchone()
            pw = la.set_temp_password(cur, user["id"])
            _log(cur, "owner", "added", "user", user["id"], f"Added {username} as {role}")
    except UniqueViolation:
        raise BadRequest("That username or email is already used.") from None
    return dict(user), pw


# --- the people ----------------------------------------------------------------------------------------------------

def list_users() -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT u.id, u.username, u.email, u.display_name, u.role, u.is_owner, u.disabled_at, u.created_at, "
            "(u.password_temp AND u.password_hash IS NOT NULL AND u.password_expires_at > now()) AS temp_pending, u.password_expires_at, "
            "(SELECT count(*) FROM webauthn_credentials w WHERE w.user_id = u.id) AS passkeys, "
            "(SELECT count(*) FROM web_sessions s WHERE s.user_id = u.id AND s.expires_at > now()) AS sessions "
            "FROM users u WHERE u.username IS NOT NULL ORDER BY (u.role = 'owner') DESC, lower(u.username)")
        return [dict(r) for r in cur.fetchall()]


def _person(cur, user_id: int) -> dict:
    cur.execute("SELECT * FROM users WHERE id = %s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    if not row or row["username"] is None:
        raise NotFound("no such user")
    return row


def _not_owner(row: dict, what: str) -> None:
    if row["role"] == access.OWNER or row["is_owner"]:
        raise Conflict(f"the owner account can't be {what} here")


def set_role(user_id: int, role: str) -> None:
    if role not in access.INVITE_ROLES:
        raise BadRequest(f"role must be one of {', '.join(access.INVITE_ROLES)}")
    with get_cursor() as cur:
        row = _person(cur, user_id)
        _not_owner(row, "changed")
        cur.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
        if role != access.DESIGNER:   # a viewer deploys nothing: server assignments go with the right to deploy
            cur.execute("DELETE FROM server_assignments WHERE user_id = %s", (user_id,))
        _log(cur, "owner", "role", "user", user_id,
                             f"{row['username']} is now a {role}")
        devices_to_revoke = []
        if role == access.VIEWER:   # a viewer's runners (and so their CA turns and jobs) stop with the role
            cur.execute("SELECT id, account_id FROM device_tokens WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
            devices_to_revoke = [(r["account_id"], r["id"]) for r in cur.fetchall()]
    from app import devices   # lazy: devices imports jobs/ca, which import users' neighbours
    for account_id, device_id in devices_to_revoke:
        devices.revoke(account_id, device_id, "owner (role changed to viewer)", user_id=user_id)


def set_disabled(user_id: int, disabled: bool) -> None:
    with get_cursor() as cur:
        row = _person(cur, user_id)
        _not_owner(row, "disabled")
        cur.execute("UPDATE users SET disabled_at = CASE WHEN %s THEN now() END, session_epoch = session_epoch + 1 "
                    "WHERE id = %s", (disabled, user_id))
        if disabled:
            cur.execute("DELETE FROM web_sessions WHERE user_id = %s", (user_id,))
        _log(cur, "owner", "disabled" if disabled else "enabled", "user", user_id,
                             f"{row['username']} {'disabled' if disabled else 'enabled'}")


def revoke_sessions(user_id: int) -> None:
    """Sign them out everywhere: the epoch moves (every older session is dead) and their rows go."""
    with get_cursor() as cur:
        row = _person(cur, user_id)
        cur.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = %s", (user_id,))
        cur.execute("DELETE FROM web_sessions WHERE user_id = %s", (user_id,))
        _log(cur, "owner", "signed out", "user", user_id,
                             f"Ended every session of {row['username']}")


def reset_signin(user_id: int) -> str:
    """Remove the person's password, passkeys, authenticator and recovery codes, end every session, and return a NEW temporary password (24 h,
    once, shown to the owner). They sign in with it and set up password + authenticator again. Refused for the owner account."""
    with get_cursor() as cur:
        row = _person(cur, user_id)
        _not_owner(row, "reset here (use `python -m app.cli reset-owner` on the server)")
        pw = la.reset_signin_in(cur, user_id)
        _log(cur, "owner", "reset", "user", user_id, f"Reset the sign-in of {row['username']}")
    return pw


def clear_email(user_id: int) -> None:
    """Remove a person's sign-in email (an identifier only; nothing is ever sent to it)."""
    with get_cursor() as cur:
        row = _person(cur, user_id)
        cur.execute("UPDATE users SET email = NULL WHERE id = %s", (user_id,))
        _log(cur, "owner", "email cleared", "user", user_id, f"Cleared the sign-in email of {row['username']}")
