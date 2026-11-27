"""Postgres access — the dashboard's OWN database. Never the ERP's or staging's."""
from __future__ import annotations

import glob
import os
import time
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False)


@contextmanager
def get_cursor(commit: bool = True):
    """Yield a dict-row cursor on its own connection; commits on clean exit."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def wait_for_db(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            with connect() as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1)
    raise RuntimeError(f"database never became ready: {last_err}")


def _run_python_migration(conn, path: str) -> None:
    """Applies an existing .py migration (the five old data migrations). It defines `migrate(conn)`, runs inside the same transaction as
    its schema_migrations row, and must not commit. Do NOT add new ones: a migration changes the schema only (scripts/migration_lint.py
    refuses a new .py file). A data change goes through app code (e.g. a startup task) or the private instance repo."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"_migration_{os.path.basename(path)[:-3]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.migrate(conn)


def run_migrations(migrations_dir: str = "db/migrations") -> list[str]:
    """Apply every .sql / .py file in filename order, tracked in schema_migrations. Idempotent."""
    applied = []
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
        paths = glob.glob(os.path.join(migrations_dir, "*.sql")) + glob.glob(os.path.join(migrations_dir, "*.py"))
        for path in sorted(paths, key=os.path.basename):
            name = os.path.basename(path)
            if name in done:
                continue
            if name.endswith(".py"):
                _run_python_migration(conn, path)
            else:
                with open(path, encoding="utf-8") as fh:
                    conn.execute(fh.read())
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (name,))
            conn.commit()
            applied.append(name)
    return applied


def rollback_python_migration(filename: str, migrations_dir: str = "db/migrations") -> None:
    """Run the documented `rollback(conn)` of a .py DATA migration that ships one (e.g. 0023_account_sessions.py),
    in one transaction. The schema_migrations row is left alone, so the migration doesn't re-apply on the next
    start. CLI (inside the app container): python -m app.db rollback 0023_account_sessions.py"""
    import importlib.util

    name = os.path.basename(filename)
    path = os.path.join(migrations_dir, name)
    if not name.endswith(".py") or not os.path.isfile(path):
        raise ValueError(f"no python migration {name!r} in {migrations_dir}")
    spec = importlib.util.spec_from_file_location(f"_migration_{name[:-3]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "rollback"):
        raise ValueError(f"{name} has no rollback(conn)")
    with connect() as conn:
        module.rollback(conn)
        conn.commit()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "rollback":
        print("usage: python -m app.db rollback <migration>.py", file=sys.stderr)
        sys.exit(2)
    rollback_python_migration(sys.argv[2])
    print(f"rolled back {sys.argv[2]}")
