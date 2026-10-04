"""The robustness fields of a metrics object (docs/ROBUSTNESS.md, A2) built from the
per-game records of a backtest: bootstrap CIs, the market test, the Brier
decomposition, the recalibration fit, the shrunk ROI and the era label; plus the
overfit rule of A1 that compares a search-era and a validation-era metrics object.
"""

from __future__ import annotations

import random
from typing import Any

from fleet.sim.metrics import shrunk_roi
from fleet.sim.records import bet_rows, ll_gains
from fleet.sim.stats import (B_DEFAULT, N_FLIPS_DEFAULT, bet_bootstrap, brier_decomposition, drawdown_bootstrap,
                             logistic_recalibration, permutation_market_test)

OVERFIT_ROI_GAP = 0.03
OVERFIT_MARKET_P = 0.1


def capital_cents(limits: dict[str, Any]) -> int:
    return int(limits.get("default_bankroll_cents", 10000)) * int(limits.get("trade_max_games", 6))


def robust_fields(per_season_records: list[list[dict[str, Any]]], limits: dict[str, Any],
                  seed: int | str, era: str, metrics: dict[str, Any],
                  B: int = B_DEFAULT, n_flips: int = N_FLIPS_DEFAULT) -> dict[str, Any]:
    """ci, mean_ll_gain, market_p, brier_decomposition, calib_slope, calib_intercept,
    shrunk_roi and era for a backtest whose scored games are per_season_records.

    Bootstrap draws come from random.Random(f"{seed}:boot") (bets first, then the
    season blocks for the drawdown), the sign flips from random.Random(f"{seed}:perm")."""
    records = [r for season in per_season_records for r in season]
    boot = random.Random(f"{seed}:boot")
    ci = bet_bootstrap(bet_rows(records), B, boot)
    capital = capital_cents(limits)
    dd = drawdown_bootstrap([[r["pnl_cents"] for r in season] for season in per_season_records], B, boot)
    ci["max_drawdown"] = [dd[0] / capital, dd[1] / capital] if capital > 0 else [None, None]
    mean_gain, market_p = permutation_market_test(ll_gains(records), n_flips, random.Random(f"{seed}:perm"))
    p = [r["p"] for r in records]
    outcomes = [r["outcome"] for r in records]
    slope, intercept = logistic_recalibration(p, outcomes)
    return {
        "ci": {key: ci[key] for key in ("roi", "avg_clv", "max_drawdown", "hit_rate", "avg_edge")},
        "mean_ll_gain": mean_gain,
        "market_p": market_p,
        "brier_decomposition": brier_decomposition(p, outcomes),
        "calib_slope": slope,
        "calib_intercept": intercept,
        "shrunk_roi": shrunk_roi(metrics),
        "era": era,
    }


def _shrunk(metrics: dict[str, Any]) -> float:
    value = metrics.get("shrunk_roi")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return shrunk_roi(metrics)


def beats_market(metrics: dict[str, Any] | None, threshold: float = OVERFIT_MARKET_P) -> bool:
    """True when the era's market test is below the threshold (a missing test never beats)."""
    if not metrics:
        return False
    p = metrics.get("market_p")
    return isinstance(p, (int, float)) and not isinstance(p, bool) and float(p) < threshold


def overfit_flags(search_metrics: dict[str, Any] | None, validation_metrics: dict[str, Any]) -> list[str]:
    """["overfit"] when the search era's shrunk ROI exceeds the validation era's by more
    than 0.03, or the search era beats the market (p < 0.1) and the validation era does
    not; [] otherwise or without search-era metrics."""
    if not search_metrics:
        return []
    if _shrunk(search_metrics) - _shrunk(validation_metrics) > OVERFIT_ROI_GAP:
        return ["overfit"]
    if beats_market(search_metrics) and not beats_market(validation_metrics):
        return ["overfit"]
    return []
