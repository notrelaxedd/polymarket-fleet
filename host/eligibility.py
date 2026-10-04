"""Lineage eligibility (docs/MODELS.md, "Eligibility").

A lineage's status comes from its root model's backtest metrics against the
`thresholds_backtest` settings: candidate -> paper_ok when every threshold holds,
back to candidate when they stop holding. paper_ok -> live_eligible is step 4 (an
existing live_eligible row keeps it while the backtest thresholds still hold). A
retired lineage stays retired. The status is written to every row of the lineage.
Whenever a lineage leaves `live_eligible` (a thresholds change, a new backtest result,
the paper gate, a retirement) its active live assignments are halted, which cancels
their orders (docs/LIVE.md: "demoting a lineage halts them").

`thresholds` reads the setting FOR SHARE, and every model write reads it before it
touches a model row: a write that overlaps a thresholds change waits for the new
value, and the thresholds UPDATE waits for in-flight model writes, so the
recompute_all that follows it sees their rows.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.events import add_audit

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


def recompute_lineage(
    conn: psycopg.Connection, lineage_id: Any, limits: dict[str, Any] | None = None, actor: str | None = "eligibility",
) -> str | None:
    """Recompute and store the status of every row of a lineage; the new status, or
    None when the lineage has no root row. `limits` are the thresholds already read
    by the caller (before it locked any model row), else they are read here. A
    lineage that was live_eligible and no longer is gets its live assignments halted
    (under the live approval lock, taken before the model rows as the kill and the
    settlement do)."""
    from host.kill import approval_lock

    was = conn.execute("SELECT status FROM models WHERE id = %s", (lineage_id,)).fetchone()
    if was is not None and was["status"] == "live_eligible":
        approval_lock(conn, "live")
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
    if current == "live_eligible" and new != "live_eligible":
        add_audit(conn, "eligibility_changed", str(lineage_id), actor, {"status": current}, {"status": new})
        halt_live_assignments(conn, lineage_id, actor, "lineage no longer live_eligible")
    return new


def recompute_all(conn: psycopg.Connection) -> int:
    """Recompute every lineage (after a thresholds change); the number of lineages."""
    limits = thresholds(conn)
    rows = conn.execute("SELECT DISTINCT lineage_id FROM models").fetchall()
    for row in rows:
        recompute_lineage(conn, row["lineage_id"], limits)
    return len(rows)


# ------------------------------------------------------------ step 4: paper gate

DEFAULT_PAPER_THRESHOLDS: dict[str, Any] = {
    "min_games": 10, "min_bets": 40, "min_days": 21, "min_clv": 0.0, "min_pnl_cents": 1,
}


def paper_thresholds(conn: psycopg.Connection) -> dict[str, Any]:
    """`settings.thresholds_paper` (read FOR SHARE) with defaults for missing keys."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'thresholds_paper' FOR SHARE").fetchone()
    value = None if row is None else row["value"]
    out = dict(DEFAULT_PAPER_THRESHOLDS)
    if isinstance(value, dict):
        out.update({k: v for k, v in value.items() if k in out and v is not None})
    return out


def paper_stats(conn: psycopg.Connection, lineage_id: Any, now: Any = None) -> dict[str, Any]:
    """The lineage's pooled paper record: distinct games (one game traded by several
    models of the lineage counts once; a settled game with no bet still counts) and
    bets from model_scores, stake weighted CLV, P&L and days since its first paper bet."""
    row = conn.execute(
        """
        SELECT count(DISTINCT game_id) AS games, COALESCE(SUM(n_bets), 0) AS bets, COALESCE(SUM(pnl_cents), 0) AS pnl_cents,
               SUM(CASE WHEN avg_clv IS NOT NULL THEN avg_clv * stake_cents END) AS clv_weight,
               SUM(CASE WHEN avg_clv IS NOT NULL THEN stake_cents END) AS clv_stake
          FROM model_scores WHERE lineage_id = %s AND mode = 'paper'
        """,
        (lineage_id,),
    ).fetchone()
    first = conn.execute(
        "SELECT MIN(settled_at) AS first FROM bets WHERE lineage_id = %s AND mode = 'paper'", (lineage_id,)
    ).fetchone()["first"]
    days = 0.0
    if first is not None:
        current = now or conn.execute("SELECT now() AS now").fetchone()["now"]
        days = max(0.0, (current - first).total_seconds() / 86400.0)
    stake = float(row["clv_stake"] or 0)
    avg_clv = float(row["clv_weight"]) / stake if stake > 0 else None
    return {
        "games": int(row["games"]), "bets": int(row["bets"]), "pnl_cents": int(row["pnl_cents"]),
        "avg_clv": avg_clv, "days": days,
    }


def meets_paper_thresholds(stats: dict[str, Any], limits: dict[str, Any]) -> bool:
    """games, bets, days, avg_clv and pnl each at or above its threshold; a lineage
    with no CLV yet never passes."""
    if stats.get("avg_clv") is None:
        return False
    return (
        stats["games"] >= int(limits["min_games"])
        and stats["bets"] >= int(limits["min_bets"])
        and stats["days"] >= float(limits["min_days"])
        and float(stats["avg_clv"]) >= float(limits["min_clv"])
        and stats["pnl_cents"] >= int(limits["min_pnl_cents"])
    )


def recompute_paper(conn: psycopg.Connection, lineage_id: Any, actor: str | None = "settle") -> str | None:
    """After settlement: the backtest gate first, then `paper_ok -> live_eligible`
    when the pooled paper scores meet `thresholds_paper` and `live_eligible ->
    paper_ok` (live assignments halted, their orders cancelled) when they stop
    meeting them. The status, or None for an unknown lineage."""
    paper_limits = paper_thresholds(conn)
    current = recompute_lineage(conn, lineage_id, actor=actor)
    if current is None:
        return None
    meets = meets_paper_thresholds(paper_stats(conn, lineage_id), paper_limits)
    new = current
    if current == "paper_ok" and meets:
        new = "live_eligible"
    elif current == "live_eligible" and not meets:
        new = "paper_ok"
    if new != current:
        conn.execute(
            "UPDATE models SET status = %s, updated_at = now() WHERE lineage_id = %s AND status <> %s",
            (new, lineage_id, new),
        )
        add_audit(conn, "eligibility_changed", str(lineage_id), actor, {"status": current}, {"status": new})
        if new == "paper_ok":
            halt_live_assignments(conn, lineage_id, actor, "lineage no longer live_eligible")
    return new


def halt_live_assignments(conn: psycopg.Connection, lineage_id: Any, actor: str | None, reason: str) -> int:
    """Halt every active live assignment of the lineage (orders cancelled by halt)."""
    from host.trading import assignments

    rows = conn.execute(
        "SELECT id FROM assignments WHERE lineage_id = %s AND mode = 'live' AND status = 'active'", (lineage_id,)
    ).fetchall()
    for row in rows:
        assignments.halt_assignment(conn, row["id"], actor, reason)
    return len(rows)
