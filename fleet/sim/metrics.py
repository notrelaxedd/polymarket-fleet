"""Backtest metrics (docs/MODELS.md, "Metrics").

A `stats` dict holds the sufficient statistics of a run of games in order; stats of
consecutive seasons merge exactly (including the peak-to-trough drawdown), so a
checkpoint only needs the per-season stats to finish a backtest.
"""

from __future__ import annotations

import math
from typing import Any

N_BUCKETS = 10
LOG_EPS = 1e-12


def empty_stats() -> dict[str, Any]:
    return {
        "n_games": 0, "n_bets": 0, "hits": 0, "total_stake_cents": 0, "pnl_cents": 0,
        "sum_edge": 0.0, "sum_log_loss": 0.0, "sum_brier": 0.0, "sum_market_log_loss": 0.0,
        "calibration": [[0, 0.0, 0.0] for _ in range(N_BUCKETS)],
        "max_prefix": 0, "min_prefix": 0, "max_drawdown_cents": 0,
    }


def log_loss(p: float, outcome: float) -> float:
    p = min(max(p, LOG_EPS), 1.0 - LOG_EPS)
    return -(outcome * math.log(p) + (1.0 - outcome) * math.log(1.0 - p))



def record_game(stats: dict[str, Any], p: float, p_market: float, outcome: float,
                bet: dict[str, Any] | None, pnl_cents: int) -> None:
    """Score one moneyline game (and its bet, if any) into stats, in game order."""
    stats["n_games"] += 1
    stats["sum_log_loss"] += log_loss(p, outcome)
    stats["sum_market_log_loss"] += log_loss(p_market, outcome)
    stats["sum_brier"] += (p - outcome) ** 2
    bucket = stats["calibration"][min(int(p * N_BUCKETS), N_BUCKETS - 1)]
    bucket[0] += 1
    bucket[1] += p
    bucket[2] += outcome
    if bet is None:
        return
    stats["n_bets"] += 1
    stats["total_stake_cents"] += int(bet["stake_cents"])
    stats["sum_edge"] += float(bet["edge"])
    if pnl_cents > 0:
        stats["hits"] += 1
    cum = stats["pnl_cents"] + int(pnl_cents)
    stats["pnl_cents"] = cum
    stats["max_prefix"] = max(stats["max_prefix"], cum)
    stats["min_prefix"] = min(stats["min_prefix"], cum)
    stats["max_drawdown_cents"] = max(stats["max_drawdown_cents"], stats["max_prefix"] - cum)


def merge_stats(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Stats of the concatenation of consecutive runs."""
    out = empty_stats()
    offset = 0
    peak = 0
    for s in parts:
        for key in ("n_games", "n_bets", "hits", "total_stake_cents", "sum_edge",
                    "sum_log_loss", "sum_brier", "sum_market_log_loss"):
            out[key] += s[key]
        for i, (count, sum_p, sum_y) in enumerate(s["calibration"]):
            out["calibration"][i][0] += count
            out["calibration"][i][1] += sum_p
            out["calibration"][i][2] += sum_y
        dd = max(s["max_drawdown_cents"], peak - (offset + s["min_prefix"]))
        out["max_drawdown_cents"] = max(out["max_drawdown_cents"], dd)
        peak = max(peak, offset + s["max_prefix"])
        offset += s["pnl_cents"]
    out["pnl_cents"] = offset
    out["max_prefix"] = peak
    out["min_prefix"] = min([0] + [sum(p["pnl_cents"] for p in parts[:i]) + parts[i]["min_prefix"]
                                   for i in range(len(parts))])
    return out


def metrics_from_stats(stats: dict[str, Any], limits: dict[str, Any], seasons: list[int]) -> dict[str, Any]:
    """The metrics object of docs/MODELS.md from sufficient statistics."""
    n_games = stats["n_games"]
    n_bets = stats["n_bets"]
    stake = stats["total_stake_cents"]
    capital = int(limits.get("default_bankroll_cents", 10000)) * int(limits.get("trade_max_games", 6))
    calibration = [
        {"count": c, "mean_p": (sp / c) if c else 0.0, "mean_outcome": (sy / c) if c else 0.0}
        for c, sp, sy in stats["calibration"]
    ]
    return {
        "n_games": n_games,
        "n_bets": n_bets,
        "total_stake_cents": stake,
        "pnl_cents": stats["pnl_cents"],
        "roi": (stats["pnl_cents"] / stake) if stake else 0.0,
        "hit_rate": (stats["hits"] / n_bets) if n_bets else 0.0,
        "avg_edge": (stats["sum_edge"] / n_bets) if n_bets else 0.0,
        "avg_stake_cents": (stake / n_bets) if n_bets else 0.0,
        "log_loss": (stats["sum_log_loss"] / n_games) if n_games else 0.0,
        "brier": (stats["sum_brier"] / n_games) if n_games else 0.0,
        "market_log_loss": (stats["sum_market_log_loss"] / n_games) if n_games else 0.0,
        "calibration": calibration,
        "max_drawdown_cents": stats["max_drawdown_cents"],
        # None (not 0) when the capital is 0: a missing metric never passes a threshold.
        "max_drawdown": (stats["max_drawdown_cents"] / capital) if capital > 0 else None,
        "seasons": list(seasons),
    }


def shrunk_roi(metrics: dict[str, Any]) -> float:
    """The search objective: roi * n_bets / (n_bets + 100)."""
    n = metrics.get("n_bets", 0)
    return float(metrics.get("roi", 0.0)) * n / (n + 100)
