"""Stress tests over a validation-era base run (docs/ROBUSTNESS.md, A3): the price
stress, the neighbourhood summary, the regime table and the fragile and
regime_dependent flags. Everything here works on records (fleet.sim.records); the
neighbourhood backtests themselves are run by fleet.sim.validate.

Price stress: the fee model only enters the fill rule (model probabilities never see
it), so re-planning every bet of the base run's records under the changed rule is the
same backtest as a full re-run with the changed fee model, without the replay. A
snapshot replay record is filled again on its stored price facts with the spread
change added to the entry price (fleet.sim.prices).
"""

from __future__ import annotations

from typing import Any

from fleet.sim.fills import BetRule
from fleet.sim.metrics import empty_stats, metrics_from_stats, record_game, shrunk_roi
from fleet.sim.records import REGIME_DIMENSIONS, mean_ll_gain, regimes_of, replan
from fleet.sim.stats import percentile

PRICE_STRESSES: tuple[tuple[str, dict[str, float]], ...] = (
    ("spread+0.01", {"half_spread": 0.01}),
    ("spread+0.02", {"half_spread": 0.02}),
    ("fee x1.5", {"taker_rate": 1.5}),
)
NEIGHBOURHOOD_N = 10
FRAGILE_BET_FRACTION = 0.5
REGIME_PROFIT_SHARE = 0.8


def stressed_rule(params: dict[str, Any], limits: dict[str, Any], change: dict[str, float]) -> BetRule:
    """The fill rule with half_spread raised by, or taker_rate multiplied by, the change.
    The spread change also becomes price_bump, which a snapshot replay record adds to
    every entry price (its recorded ask already holds the spread)."""
    base = BetRule.build(params, limits)
    return BetRule(
        taker_rate=base.taker_rate * change.get("taker_rate", 1.0),
        half_spread=base.half_spread + change.get("half_spread", 0.0),
        min_edge=base.min_edge, kelly_fraction=base.kelly_fraction,
        bankroll_cents=base.bankroll_cents, max_bet_cents=base.max_bet_cents,
        participation=base.participation, price_bump=base.price_bump + change.get("half_spread", 0.0),
    )


def _summary(records: list[dict[str, Any]], limits: dict[str, Any]) -> dict[str, Any]:
    stats = empty_stats()
    for r in records:
        record_game(stats, r["p"], r["p_market"], r["outcome"], r["bet"], r["pnl_cents"])
    return metrics_from_stats(stats, limits, [])


def price_stress(records: list[dict[str, Any]], params: dict[str, Any], limits: dict[str, Any]) -> list[dict[str, Any]]:
    """[{"name", "n_bets", "roi", "log_loss", "mean_ll_gain"}] for the three price changes."""
    out = []
    for name, change in PRICE_STRESSES:
        rule = stressed_rule(params, limits, change)
        stressed = [replan(r, rule) for r in records]
        m = _summary(stressed, limits)
        out.append({"name": name, "n_bets": m["n_bets"], "roi": m["roi"], "log_loss": m["log_loss"],
                    "mean_ll_gain": mean_ll_gain(stressed)})
    return out


def neighbourhood_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Median and 10th percentile of shrunk ROI and log-loss gain over the perturbed runs
    ({"shrunk_roi", "mean_ll_gain"} each)."""
    rois = [float(r["shrunk_roi"]) for r in runs]
    gains = [float(r["mean_ll_gain"]) for r in runs]
    return {
        "n": len(runs),
        "shrunk_roi_median": percentile(rois, 0.5), "shrunk_roi_p10": percentile(rois, 0.1),
        "ll_gain_median": percentile(gains, 0.5), "ll_gain_p10": percentile(gains, 0.1),
    }


def regime_table(records: list[dict[str, Any]], limits: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per regime name: n_games, n_bets, roi, pnl_cents, mean_ll_gain. Every scored game
    falls in exactly one regime of each dimension (fleet.sim.records.regimes_of)."""
    groups: dict[str, list[dict[str, Any]]] = {name: [] for pair in REGIME_DIMENSIONS for name in pair}
    for r in records:
        for name in regimes_of(r):
            groups[name].append(r)
    out = {}
    for name, members in groups.items():
        m = _summary(members, limits)
        out[name] = {"n_games": m["n_games"], "n_bets": m["n_bets"], "roi": m["roi"], "pnl_cents": m["pnl_cents"],
                     "mean_ll_gain": mean_ll_gain(members)}
    return out


def fragile_flag(base: dict[str, Any], prices: list[dict[str, Any]], neighbourhood: dict[str, Any]) -> bool:
    """The +0.02 spread run keeps fewer than half the base bets or turns a positive base
    ROI negative, or the neighbourhood median shrunk ROI is below zero while the base's
    is above."""
    spread2 = next((p for p in prices if p["name"] == "spread+0.02"), None)
    base_bets = int(base.get("n_bets", 0))
    base_roi = float(base.get("roi", 0.0))
    if spread2 is not None and base_bets > 0:
        if int(spread2["n_bets"]) < FRAGILE_BET_FRACTION * base_bets:
            return True
        if base_roi > 0 and float(spread2["roi"]) < 0:
            return True
    if neighbourhood.get("n") and shrunk_roi(base) > 0 and float(neighbourhood["shrunk_roi_median"]) < 0:
        return True
    return False


def regime_dependent_flag(regimes: dict[str, dict[str, Any]]) -> bool:
    """One regime holds more than 80% of the profit while the rest of its dimension
    loses money (checked per dimension on a positive total)."""
    for pair in REGIME_DIMENSIONS:
        pnls = [int(regimes.get(name, {}).get("pnl_cents", 0)) for name in pair]
        total = sum(pnls)
        if total <= 0:
            continue
        for mine, other in ((pnls[0], pnls[1]), (pnls[1], pnls[0])):
            if mine > REGIME_PROFIT_SHARE * total and other < 0:
                return True
    return False


def stress_flags(base: dict[str, Any], prices: list[dict[str, Any]], neighbourhood: dict[str, Any],
                 regimes: dict[str, dict[str, Any]]) -> list[str]:
    flags = []
    if fragile_flag(base, prices, neighbourhood):
        flags.append("fragile")
    if regime_dependent_flag(regimes):
        flags.append("regime_dependent")
    return flags
