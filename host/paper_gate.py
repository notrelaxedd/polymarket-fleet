"""The paper gate: paper_ok -> live_eligible from a lineage's pooled paper record
(docs/TRADING.md "Settlement, bets, scoring, eligibility") and, since step 6, the
paper CLV bootstrap (docs/ROBUSTNESS.md A2 and A4).

`thresholds_paper`: `min_games`, `min_bets`, `min_days`, `min_clv`, `min_pnl_cents`
over the pooled record, and `clv_ci_excludes_zero`: the 5th percentile of the
bootstrapped stake-weighted average CLV over the lineage's settled paper bets (B =
1000, seeded `paper:<lineage_id>`) must be above 0, over at least `min_bets` bets with
a CLV. The interval is cached in `lineage_paper_ci` on every recompute so the
leaderboard can show it.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.events import add_audit
from host.stats import bootstrap_ci, weighted_mean

DEFAULT_PAPER_THRESHOLDS: dict[str, Any] = {
    "min_games": 10, "min_bets": 40, "min_days": 21, "min_clv": 0.0, "min_pnl_cents": 1, "clv_ci_excludes_zero": True,
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


def paper_clv_ci(bets: list[tuple[float, float]], lineage_id: Any) -> dict[str, Any]:
    """{n_bets, avg_clv, ci: [low, high] | None} over (clv, stake_cents) pairs: the
    stake-weighted average CLV and its bootstrap interval seeded `paper:<lineage_id>`."""
    values = [float(clv) for clv, _ in bets]
    weights = [max(float(stake), 0.0) for _, stake in bets]
    if not values:
        return {"n_bets": 0, "avg_clv": None, "ci": None}
    return {
        "n_bets": len(values), "avg_clv": weighted_mean(values, weights),
        "ci": bootstrap_ci(values, f"paper:{lineage_id}", weights),
    }


def paper_ci(conn: psycopg.Connection, lineage_id: Any) -> dict[str, Any]:
    """Compute the lineage's paper CLV interval from its settled paper bets with a
    CLV (in settlement order), cache it in lineage_paper_ci and return it."""
    rows = conn.execute(
        "SELECT clv, stake_cents FROM bets WHERE lineage_id = %s AND mode = 'paper' AND clv IS NOT NULL ORDER BY settled_at, id",
        (lineage_id,),
    ).fetchall()
    out = paper_clv_ci([(r["clv"], r["stake_cents"]) for r in rows], lineage_id)
    low, high = (out["ci"] or (None, None))
    conn.execute(
        """
        INSERT INTO lineage_paper_ci (lineage_id, n_bets, avg_clv, clv_low, clv_high, computed_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (lineage_id) DO UPDATE SET n_bets = EXCLUDED.n_bets, avg_clv = EXCLUDED.avg_clv,
               clv_low = EXCLUDED.clv_low, clv_high = EXCLUDED.clv_high, computed_at = now()
        """,
        (lineage_id, out["n_bets"], out["avg_clv"], low, high),
    )
    return out


def meets_paper_ci(ci: dict[str, Any], limits: dict[str, Any]) -> bool:
    """The `clv_ci_excludes_zero` rule: off, or the CLV 5th percentile above 0 over at
    least `min_bets` bets with a CLV."""
    if not limits.get("clv_ci_excludes_zero"):
        return True
    bounds = ci.get("ci")
    return bool(bounds) and int(ci.get("n_bets") or 0) >= int(limits["min_bets"]) and float(bounds[0]) > 0.0


def recompute_paper(conn: psycopg.Connection, lineage_id: Any, actor: str | None = "settle") -> str | None:
    """After settlement: the backtest gate first, then `paper_ok -> live_eligible`
    when the pooled paper scores meet `thresholds_paper` (and the CLV interval rule)
    and `live_eligible -> paper_ok` (live assignments halted, their orders cancelled)
    when they stop meeting them. The status, or None for an unknown lineage."""
    from host.eligibility import halt_live_assignments, recompute_lineage

    paper_limits = paper_thresholds(conn)
    current = recompute_lineage(conn, lineage_id, actor=actor)
    if current is None:
        return None
    meets = meets_paper_thresholds(paper_stats(conn, lineage_id), paper_limits) and meets_paper_ci(
        paper_ci(conn, lineage_id), paper_limits
    )
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
