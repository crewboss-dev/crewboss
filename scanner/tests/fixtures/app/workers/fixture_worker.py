"""Fixture worker mimicking app/workers/token_refresh.py's shape: an INTERVAL env-driven constant, an
ENABLED gate, app.clients imports, and a run_once() that writes a table — the pattern
discover/workers.py parses."""
import os
import time

from app import ops
from app.clients import xero

ENABLED = os.environ.get("FIXTURE_ENABLED", "").lower() in ("1", "true")
INTERVAL = float(os.environ.get("FIXTURE_WORKER_SECONDS", "600"))


def run_once():
    xero.refresh("token")
    sql = "INSERT INTO fixture_table (a, b) VALUES (%s, %s)"
    return sql


def main():
    while True:
        run_once()
        time.sleep(INTERVAL)
