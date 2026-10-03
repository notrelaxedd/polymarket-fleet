"""Profit and loss summary for the dashboard and /api/pnl.

TODO(step 4): compute today's and all-time P&L from bets and fills (settled in the
owner's time zone, settings.tz). Until then everything is zero; the shape and the
by_worker keys are already what the dashboard renders.
"""
from __future__ import annotations

from typing import Any

import psycopg


def pnl(conn: psycopg.Connection) -> dict[str, Any]:
    """{"today_cents", "all_time_cents", "by_worker": {worker_id: cents}}."""
    rows = conn.execute("SELECT id FROM workers ORDER BY id").fetchall()
    return {
        "today_cents": 0,
        "all_time_cents": 0,
        "by_worker": {row["id"]: 0 for row in rows},
    }
