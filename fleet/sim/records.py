"""Per-game backtest records: what a scored game leaves behind for the statistics
(fleet.sim.stats), the stress tests (fleet.sim.stress) and the checkpoint.

A record is a dict: game_id, season, p (model P(home)), p_market, outcome, bet (None or
{"side", "stake_cents", "edge", "clv", ...}), pnl_cents, ll_model, ll_market, lean (the
side with the larger edge, "home" or "away", whether or not it was bet), div_game,
hour_et (kickoff hour, US Eastern), temp, wind, outdoor; a snapshot replay record
(build_snapshot_record) also carries "prices", the game's price facts
(fleet.sim.prices), which a checkpoint stores next to the packed probabilities.

Everything in a record but `p` is a function of the game, the outcome and the fill
rule, so a checkpoint carries only the model probabilities of a season's scored games
in kickoff order (pack_probs: base64 of little-endian float64s, about 11 bytes a game);
fleet.sim.backtest.rebuild_records walks the same games under the same rule and gets
the identical records back.
"""

from __future__ import annotations

import base64
import math
import struct
from datetime import datetime
from typing import Any

from fleet.sim.data import _eastern
from fleet.sim.fills import BetRule, best_side, plan_bet, settle
from fleet.sim.metrics import log_loss
from fleet.sim.prices import lean_of, p_market_of, plan_replay_bet

INDOOR_ROOFS = ("dome", "closed")
_TZ = _eastern()


def kickoff_hour_et(kickoff_at: str) -> int:
    """The kickoff hour (0-23) in US Eastern time from the UTC ISO kickoff."""
    moment = datetime.fromisoformat(kickoff_at)
    return moment.astimezone(_TZ).hour


def _record(game: dict[str, Any], p: float, p_market: float, outcome: float, bet: dict[str, Any] | None,
            lean: str) -> dict[str, Any]:
    pnl = settle(bet, outcome)[1] if bet else 0
    roof = (game.get("roof") or "").lower()
    return {
        "game_id": game["game_id"],
        "season": game["season"],
        "p": p,
        "p_market": p_market,
        "outcome": outcome,
        "bet": bet,
        "pnl_cents": pnl,
        "ll_model": log_loss(p, outcome),
        "ll_market": log_loss(p_market, outcome),
        "lean": lean,
        "div_game": int(game.get("div_game") or 0),
        "hour_et": kickoff_hour_et(game["kickoff_at"]),
        "temp": game.get("temp"),
        "wind": game.get("wind"),
        "outdoor": roof not in INDOOR_ROOFS,
    }


def build_record(game: dict[str, Any], p: float, p_market: float, outcome: float, rule: BetRule) -> dict[str, Any]:
    """Score one game under the fill rule: plan the bet, settle it, keep the regime features."""
    bet = plan_bet(p, p_market, rule)
    if bet is not None:
        bet["clv"] = 0.0  # entry at the close (docs/MODELS.md); snapshot replay fills a real value
    return _record(game, p, p_market, outcome, bet, best_side(p, p_market, rule)["side"])


def snapshot_lean(p: float, p_market: float, facts: dict[str, Any], rule: BetRule) -> str:
    """The buyable side with the larger edge; without an ask on either side, the side
    the model rates above the market."""
    best = lean_of(p, facts, rule)
    if best is not None:
        return str(best["side"])
    return "home" if p >= p_market else "away"


def build_snapshot_record(game: dict[str, Any], p: float, facts: dict[str, Any], outcome: float,
                          rule: BetRule) -> dict[str, Any]:
    """Score one game on its recorded prices (fleet.sim.prices): p_market from the
    decision-time mids, the bet filled on the recorded book; the record keeps the
    facts under "prices" so the price stress can fill it again."""
    p_market = p_market_of(facts)
    record = _record(game, p, p_market, outcome, plan_replay_bet(p, facts, rule), snapshot_lean(p, p_market, facts, rule))
    record["prices"] = facts
    return record


def replan(record: dict[str, Any], rule: BetRule) -> dict[str, Any]:
    """The same scored game under another fill rule (prices only enter the bet)."""
    out = dict(record)
    facts = record.get("prices")
    if facts is not None:
        bet = plan_replay_bet(record["p"], facts, rule)
    else:
        bet = plan_bet(record["p"], record["p_market"], rule)
        if bet is not None:
            bet["clv"] = 0.0
    out["bet"] = bet
    out["pnl_cents"] = settle(bet, record["outcome"])[1] if bet else 0
    return out


def pack_probs(records: list[dict[str, Any]]) -> str:
    """The model probabilities of the records, in order, as base64 of float64s
    (little-endian, so a checkpoint reads the same on every worker)."""
    probs = [float(r["p"]) for r in records]
    return base64.b64encode(struct.pack(f"<{len(probs)}d", *probs)).decode("ascii")


def unpack_probs(packed: str) -> list[float]:
    """The probabilities pack_probs encoded; ValueError on a malformed string."""
    if not isinstance(packed, str):
        raise ValueError("packed probabilities must be a string")
    data = base64.b64decode(packed.encode("ascii"), validate=True)
    if len(data) % 8:
        raise ValueError("packed probabilities are not whole float64s")
    return list(struct.unpack(f"<{len(data) // 8}d", data))


def bet_rows(records: list[dict[str, Any]]) -> list[tuple[float, float, float, float, float]]:
    """(pnl_cents, stake_cents, hit, edge, clv) per bet, in game order."""
    rows = []
    for r in records:
        bet = r.get("bet")
        if not bet:
            continue
        pnl = float(r["pnl_cents"])
        rows.append((pnl, float(bet["stake_cents"]), 1.0 if pnl > 0 else 0.0, float(bet["edge"]), float(bet.get("clv", 0.0))))
    return rows


def ll_gains(records: list[dict[str, Any]]) -> list[float]:
    """d = ll_market - ll_model per scored game (positive when the model did better)."""
    return [r["ll_market"] - r["ll_model"] for r in records]


def mean_ll_gain(records: list[dict[str, Any]]) -> float:
    gains = ll_gains(records)
    return math.fsum(gains) / len(gains) if gains else 0.0


# regimes ------------------------------------------------------------------------

REGIME_DIMENSIONS = (
    ("favourite", "underdog"), ("home", "away"), ("divisional", "non_divisional"),
    ("primetime", "day"), ("cold_or_windy", "other_weather"),
)
PRIMETIME_HOUR = 20
COLD_F = 35
WINDY_MPH = 15


def regimes_of(record: dict[str, Any]) -> list[str]:
    """One regime name per dimension: the bet side (or the model's lean without a bet)
    decides favourite/underdog (market p of that side >= 0.5) and home/away."""
    side = record["lean"]
    p_side = record["p_market"] if side == "home" else 1.0 - record["p_market"]
    temp, wind = record.get("temp"), record.get("wind")
    cold = temp is not None and temp < COLD_F
    windy = wind is not None and wind >= WINDY_MPH
    return [
        "favourite" if p_side >= 0.5 else "underdog",
        "home" if side == "home" else "away",
        "divisional" if record["div_game"] else "non_divisional",
        "primetime" if record["hour_et"] >= PRIMETIME_HOUR else "day",
        "cold_or_windy" if record["outdoor"] and (cold or windy) else "other_weather",
    ]
