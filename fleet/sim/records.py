"""Per-game backtest records: what a scored game leaves behind for the statistics
(fleet.sim.stats), the stress tests (fleet.sim.stress) and the checkpoint.

A record is a dict: game_id, season, p (model P(home)), p_market, outcome, bet (None or
{"side", "stake_cents", "edge", "clv", ...}), pnl_cents, ll_model, ll_market, lean (the
side with the larger edge, "home" or "away", whether or not it was bet), div_game,
hour_et (kickoff hour, US Eastern), temp, wind, outdoor. Checkpoints carry the packed
form, one flat list per game (PACKED_FIELDS order), which unpacks to the same dict
minus game_id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fleet.sim.data import _eastern
from fleet.sim.fills import BetRule, best_side, plan_bet, settle
from fleet.sim.metrics import log_loss

PACKED_FIELDS = ("p", "p_market", "outcome", "pnl_cents", "stake_cents", "edge", "clv", "side", "lean",
                 "div_game", "hour_et", "temp", "wind", "outdoor")
SIDE_CODES = {None: 0, "home": 1, "away": 2}
CODE_SIDES = {0: None, 1: "home", 2: "away"}
INDOOR_ROOFS = ("dome", "closed")
_TZ = _eastern()


def kickoff_hour_et(kickoff_at: str) -> int:
    """The kickoff hour (0-23) in US Eastern time from the UTC ISO kickoff."""
    moment = datetime.fromisoformat(kickoff_at)
    return moment.astimezone(_TZ).hour


def build_record(game: dict[str, Any], p: float, p_market: float, outcome: float, rule: BetRule) -> dict[str, Any]:
    """Score one game under the fill rule: plan the bet, settle it, keep the regime features."""
    bet = plan_bet(p, p_market, rule)
    if bet is not None:
        bet["clv"] = 0.0  # entry at the close (docs/MODELS.md); snapshot replay fills a real value
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
        "lean": best_side(p, p_market, rule)["side"],
        "div_game": int(game.get("div_game") or 0),
        "hour_et": kickoff_hour_et(game["kickoff_at"]),
        "temp": game.get("temp"),
        "wind": game.get("wind"),
        "outdoor": roof not in INDOOR_ROOFS,
    }


def replan(record: dict[str, Any], rule: BetRule) -> dict[str, Any]:
    """The same scored game under another fill rule (prices only enter the bet)."""
    out = dict(record)
    bet = plan_bet(record["p"], record["p_market"], rule)
    if bet is not None:
        bet["clv"] = 0.0
    out["bet"] = bet
    out["pnl_cents"] = settle(bet, record["outcome"])[1] if bet else 0
    return out


def pack(record: dict[str, Any]) -> list[Any]:
    bet = record.get("bet") or {}
    return [
        record["p"], record["p_market"], record["outcome"], record["pnl_cents"],
        int(bet.get("stake_cents", 0)) if bet else 0, float(bet.get("edge", 0.0)) if bet else 0.0,
        float(bet.get("clv", 0.0)) if bet else 0.0, SIDE_CODES[bet.get("side") if bet else None],
        SIDE_CODES[record["lean"]], int(record["div_game"]), int(record["hour_et"]),
        record.get("temp"), record.get("wind"), 1 if record["outdoor"] else 0,
    ]


def unpack(row: list[Any], season: int) -> dict[str, Any]:
    p, p_market, outcome, pnl, stake, edge, clv, side, lean, div, hour, temp, wind, outdoor = row
    bet = None
    if side:
        bet = {"side": CODE_SIDES[int(side)], "stake_cents": int(stake), "edge": float(edge), "clv": float(clv)}
    return {
        "game_id": None, "season": season, "p": p, "p_market": p_market, "outcome": outcome, "bet": bet,
        "pnl_cents": int(pnl), "ll_model": log_loss(p, outcome), "ll_market": log_loss(p_market, outcome),
        "lean": CODE_SIDES[int(lean)], "div_game": int(div), "hour_et": int(hour), "temp": temp, "wind": wind,
        "outdoor": bool(outdoor),
    }


def pack_all(records: list[dict[str, Any]]) -> list[list[Any]]:
    return [pack(r) for r in records]


def unpack_all(rows: list[list[Any]], season: int) -> list[dict[str, Any]]:
    return [unpack(r, season) for r in rows]


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
