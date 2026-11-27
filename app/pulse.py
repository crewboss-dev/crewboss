"""Business pulse — read-only signals from the ERP's production database, for PRIORITISING dev work
against the business, not for nagging. There are no invoice reminders or chasing notifications here;
that's the ERP's Now page. This module answers one question: "given what's actually happening in the
business right now, is this the highest-value thing to be working on?"

Wired in phase 3 via a read-only, column-scoped Postgres role reachable only from the dashboard host
IP (see erp-scoped-db-roles memory) — never full production credentials, and nothing here writes.
Until then every call returns None so callers (ground(), the dashboard KPI tiles, the triage context
handed to Claude) can render/ignore a "connects in phase 3" state instead of crashing.
"""
from __future__ import annotations

from typing import TypedDict


class Pulse(TypedDict, total=False):
    invoiced_this_week: float
    invoiced_this_month: float
    invoiced_prev_week: float
    invoiced_prev_month: float
    paid: float
    outstanding: float
    pipeline_value: float
    unbilled_work: float
    as_of: str


def get_pulse() -> Pulse | None:
    """None until phase 3 wires a real read-only ERP connection. Shape when live: see `Pulse` above —
    sales invoiced this week/month vs the previous period, paid vs outstanding, open pipeline value,
    and billable work not yet invoiced."""
    return None
