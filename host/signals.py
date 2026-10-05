"""Per-game signals served with the games feed and the trade state (docs/ROBUSTNESS.md B2).

game_signals(conn, game_ids) returns, per game id, {"signals": {...}, "team_stats":
{"home": [...], "away": [...]}} as the step 6 Part B contract describes. This is the
placeholder the trade state calls; the step 6B data ingest fills it in.
"""
from __future__ import annotations

from typing import Any

import psycopg


def game_signals(conn: psycopg.Connection, game_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Signals and prior team stats per game id (empty until the ingest exists)."""
    return {}
