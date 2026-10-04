"""The closing-line fill rule and stake maths (docs/MODELS.md, "Betting rule").

Prices are probabilities in (0, 1) per $1 contract; stakes and pnl are integer cents.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

DEFAULT_FEE_MODEL = {"taker_rate": 0.05, "half_spread": 0.01}


@dataclass(frozen=True)
class BetRule:
    taker_rate: float
    half_spread: float
    min_edge: float
    kelly_fraction: float
    bankroll_cents: int
    max_bet_cents: int

    @classmethod
    def build(cls, params: dict[str, Any], limits: dict[str, Any]) -> "BetRule":
        fee = dict(DEFAULT_FEE_MODEL)
        fee.update(limits.get("fee_model") or {})
        return cls(
            taker_rate=float(fee["taker_rate"]),
            half_spread=float(fee["half_spread"]),
            min_edge=float(params.get("min_edge", 0.03)),
            kelly_fraction=float(params.get("kelly_fraction", 0.25)),
            bankroll_cents=int(limits.get("default_bankroll_cents", 10000)),
            max_bet_cents=int(limits.get("max_bet_cents", 2500)),
        )


def side_cost(p_market_side: float, rule: BetRule) -> tuple[float, float, float]:
    """(price, fee, cost) of one side at the close plus half the spread and the taker fee."""
    price = p_market_side + rule.half_spread
    fee = rule.taker_rate * price * (1.0 - price)
    return price, fee, price + fee


def stake_cents(edge: float, cost: float, rule: BetRule) -> int:
    """floor(kelly * bankroll * edge / (1 - cost)), capped at max_bet and the bankroll."""
    if cost >= 1.0 or edge <= 0:
        return 0
    raw = math.floor(rule.kelly_fraction * rule.bankroll_cents * edge / (1.0 - cost))
    return max(0, min(raw, rule.max_bet_cents, rule.bankroll_cents))


def best_side(p_model: float, p_market: float, rule: BetRule) -> dict[str, Any]:
    """The side with the larger edge after costs (home wins ties):
    {"side", "p_model", "price", "fee", "cost", "edge"}, whether or not it is worth a bet."""
    best: dict[str, Any] | None = None
    for side, pm, pk in (("home", p_market, p_model), ("away", 1.0 - p_market, 1.0 - p_model)):
        price, fee, cost = side_cost(pm, rule)
        edge = pk - cost
        if best is None or edge > best["edge"]:
            best = {"side": side, "p_model": pk, "price": price, "fee": fee, "cost": cost, "edge": edge}
    assert best is not None
    return best


def plan_bet(p_model: float, p_market: float, rule: BetRule) -> dict[str, Any] | None:
    """The one bet (or None) for a game: the side with the larger edge when edge >= min_edge.

    Returns {"side", "p_model", "price", "fee", "cost", "edge", "stake_cents", "contracts"}.
    """
    best = best_side(p_model, p_market, rule)
    if best["edge"] < rule.min_edge:
        return None
    stake = stake_cents(best["edge"], best["cost"], rule)
    if stake <= 0:
        return None
    best["stake_cents"] = stake
    best["contracts"] = stake / (best["cost"] * 100.0)
    return best


def round_cents(value: float) -> int:
    return math.floor(value + 0.5)


def settle(bet: dict[str, Any], outcome: float) -> tuple[int, int]:
    """(payout_cents, pnl_cents) given the game outcome (1 home win, 0.5 tie, 0 away win)."""
    stake = int(bet["stake_cents"])
    if outcome == 0.5:
        return stake, 0
    won = (outcome == 1.0) == (bet["side"] == "home")
    if not won:
        return 0, -stake
    payout = round_cents(bet["contracts"] * 100.0)
    return payout, payout - stake
