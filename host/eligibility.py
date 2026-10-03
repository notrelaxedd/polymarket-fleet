"""Lineage eligibility (docs/MODELS.md, "Eligibility").

A lineage's status comes from its root model's backtest metrics against the
`thresholds_backtest` settings: candidate -> paper_ok when every threshold holds,
back to candidate when they stop holding. paper_ok -> live_eligible is step 4 (an
existing live_eligible row keeps it while the backtest thresholds still hold). A
retired lineage stays retired. The status is written to every row of the lineage.

`thresholds` reads the setting FOR SHARE, and every model write reads it before it
touches a model row: a write that overlaps a thresholds change waits for the new
value, and the thresholds UPDATE waits for in-flight model writes, so the
recompute_all that follows it sees their rows.
"""
from __future__ import annotations

from typing import Any

import psycopg

DEFAULT_THRESHOLDS: dict[str, Any] = {"min_bets": 200, "min_roi": 0.02, "max_drawdown": 0.30}


def thresholds(conn: psycopg.Connection) -> dict[str, Any]:
    """The backtest thresholds in force (read FOR SHARE, see the module docstring),
    with the MODELS.md defaults for missing keys."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'thresholds_backtest' FOR SHARE").fetchone()
    value = None if row is None else row["value"]
    out = dict(DEFAULT_THRESHOLDS)
    if isinstance(value, dict):
        out.update({k: v for k, v in value.items() if k in out and v is not None})
    return out


def _number(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def meets_thresholds(metrics: dict[str, Any] | None, limits: dict[str, Any]) -> bool:
    """n_bets >= min_bets, roi >= min_roi and max_drawdown <= max_drawdown; a missing
    or malformed metric never passes."""
    if not isinstance(metrics, dict):
        return False
    n_bets, roi, drawdown = (_number(metrics, k) for k in ("n_bets", "roi", "max_drawdown"))
    if n_bets is None or roi is None or drawdown is None:
        return False
    return (
        n_bets >= float(limits["min_bets"])
        and roi >= float(limits["min_roi"])
        and drawdown <= float(limits["max_drawdown"])
    )


def status_for(current: str, metrics: dict[str, Any] | None, limits: dict[str, Any]) -> str:
    """The lineage status that follows from the root's metrics."""
    if current == "retired":
        return "retired"
    if not meets_thresholds(metrics, limits):
        return "candidate"
    return "live_eligible" if current == "live_eligible" else "paper_ok"


def recompute_lineage(conn: psycopg.Connection, lineage_id: Any, limits: dict[str, Any] | None = None) -> str | None:
    """Recompute and store the status of every row of a lineage; the new status, or
    None when the lineage has no root row. `limits` are the thresholds already read
    by the caller (before it locked any model row), else they are read here."""
    rows = conn.execute(
        "SELECT id, status, backtest_metrics FROM models WHERE lineage_id = %s FOR UPDATE", (lineage_id,)
    ).fetchall()
    root = next((r for r in rows if r["id"] == lineage_id), None)
    if root is None:
        return None
    current = "retired" if any(r["status"] == "retired" for r in rows) else root["status"]
    new = status_for(current, root["backtest_metrics"], limits if limits is not None else thresholds(conn))
    conn.execute(
        "UPDATE models SET status = %s, updated_at = now() WHERE lineage_id = %s AND status <> %s",
        (new, lineage_id, new),
    )
    return new


def recompute_all(conn: psycopg.Connection) -> int:
    """Recompute every lineage (after a thresholds change); the number of lineages."""
    limits = thresholds(conn)
    rows = conn.execute("SELECT DISTINCT lineage_id FROM models").fetchall()
    for row in rows:
        recompute_lineage(conn, row["lineage_id"], limits)
    return len(rows)
