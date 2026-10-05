"""Snapshot replay prices (docs/ROBUSTNESS.md B1): the recorded prices file, the
decision-time lookup and the replayed fill.

The file is the body of GET /api/v1/data/prices: {"markets": [{"market_id", "game_id",
"side", "platform", "confirmed", "closing_price", "kickoff_at", "bars": [[minute,
bid, ask, close, min_liquidity_usd_cents], ...], "depth": [[ts, bid_depth,
ask_depth], ...]}], "count"}. Replay indexes the confirmed markets of one platform
(platform "sim" is refused unless allow_sim_prices) by game and side.

Facts: for a game, Replay.facts returns what a replayed bet needs, per side whose
market has a bar within DECISION_WINDOW_S before or at the decision time (kickoff
minus decision_minutes): the bar's mid (its close, else the bid/ask midpoint), its ask,
its min_liquidity_usd_cents, the ask levels of the last depth snapshot within
DEPTH_WINDOW_S before or at the decision time (None when there is none) and the
closing price (the market's frozen closing_price, else the last bar's mid before
kickoff). Facts are plain JSON, so a checkpoint stores them and rebuilds the records
without the prices file.

Fill: the bet buys the side with the larger edge at its ask (plus the rule's
price_bump), sized by the closing-line Kelly maths to whole contracts; with depth it
walks the ask levels at or below the ask with fleet.sim.book.walk at the rule's
participation, otherwise it fills at the ask capped by participation *
min_liquidity_usd_cents / (100 * ask) contracts. The fee is taker_rate * price *
(1 - price) per contract on the fill price; CLV = closing price - entry price.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from fleet.sim import book
from fleet.sim.fills import BetRule, round_cents, stake_cents

SIDES = ("home", "away")
SIM_PLATFORM = "sim"
DEFAULT_PLATFORM = "polymarket_us"
DEFAULT_DECISION_MINUTES = 60
MAX_DECISION_MINUTES = 300
DECISION_WINDOW_S = 30 * 60
DEPTH_WINDOW_S = 2 * 60
EPS = 1e-9


class SimPricesRefused(ValueError):
    """Platform sim prices were asked for while allow_sim_prices is off."""


def parse_ts(value: Any) -> float | None:
    """An ISO 8601 timestamp (Z or offset; naive means UTC) to epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def load_markets(path: str) -> list[dict[str, Any]]:
    """The market rows of a prices file (the feed body, or a bare list of rows)."""
    with open(path, "r", encoding="utf-8") as fh:
        body = json.load(fh)
    rows = body.get("markets") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise ValueError(f"{path}: no markets list")
    return [r for r in rows if isinstance(r, dict)]


def _bars(raw: Any) -> list[tuple[float, float | None, float | None, int | None]]:
    """(t, mid, ask, min_liquidity) per bar with a mid, sorted by time."""
    out = []
    for bar in raw or []:
        if not isinstance(bar, (list, tuple)) or len(bar) < 4:
            continue
        t = parse_ts(bar[0])
        bid, ask, close = _num(bar[1]), _num(bar[2]), _num(bar[3])
        mid = close if close is not None else ((bid + ask) / 2.0 if bid is not None and ask is not None else None)
        if t is None or mid is None:
            continue
        liq = _num(bar[4]) if len(bar) > 4 else None
        out.append((t, mid, ask, None if liq is None else int(liq)))
    out.sort(key=lambda b: b[0])
    return out


def _levels(raw: Any) -> list[list[float]]:
    out = []
    for level in raw if isinstance(raw, list) else []:
        try:
            price, size = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if math.isfinite(price) and math.isfinite(size):
            out.append([price, size])
    return out


def _depth(raw: Any) -> list[tuple[float, list[list[float]]]]:
    """(t, ask levels) per depth snapshot, sorted by time."""
    out = []
    for entry in raw or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        t = parse_ts(entry[0])
        if t is not None:
            out.append((t, _levels(entry[2])))
    out.sort(key=lambda d: d[0])
    return out


def _last_within(rows: list[Any], start: float, end: float) -> Any:
    """The last row (sorted by time) with start <= t <= end, or None."""
    found = None
    for row in rows:
        if row[0] > end + EPS:
            break
        if row[0] >= start - EPS:
            found = row
    return found


class Replay:
    """The recorded markets of one platform, indexed by game and side."""

    def __init__(self, markets: list[dict[str, Any]], platform: str, decision_minutes: int = DEFAULT_DECISION_MINUTES,
                 allow_sim: bool = False) -> None:
        if platform == SIM_PLATFORM and not allow_sim:
            raise SimPricesRefused("price platform sim is refused while allow_sim_prices is off")
        self.platform = platform
        self.decision_minutes = max(0, min(int(decision_minutes), MAX_DECISION_MINUTES))
        self.by_game: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in markets:
            if row.get("confirmed") is not True or row.get("side") not in SIDES or not row.get("game_id"):
                continue
            mine = str(row.get("platform") or platform)
            if mine != platform or (mine == SIM_PLATFORM and not allow_sim):
                continue
            entry = {"market_id": str(row.get("market_id") or ""), "closing_price": _num(row.get("closing_price")),
                     "bars": _bars(row.get("bars")), "depth": _depth(row.get("depth"))}
            self.by_game.setdefault(str(row["game_id"]), {}).setdefault(row["side"], []).append(entry)
        for sides in self.by_game.values():
            for rows in sides.values():
                rows.sort(key=lambda m: m["market_id"])

    def has_game(self, game_id: str) -> bool:
        return game_id in self.by_game

    def decision_ts(self, kickoff_at: str) -> float | None:
        kickoff = parse_ts(kickoff_at)
        return None if kickoff is None else kickoff - 60.0 * self.decision_minutes

    def facts(self, game: dict[str, Any]) -> dict[str, Any] | None:
        """{"game_id", "sides": {side: {...}}} for a game with a usable bar on a side, else None."""
        sides = self.by_game.get(str(game["game_id"]))
        decision = self.decision_ts(game["kickoff_at"])
        if not sides or decision is None:
            return None
        out: dict[str, Any] = {}
        for side in SIDES:
            for market in sides.get(side, []):
                fact = _side_facts(market, decision, decision + 60.0 * self.decision_minutes)
                if fact is not None:
                    out[side] = fact
                    break
        return {"game_id": str(game["game_id"]), "sides": out} if out else None


def _side_facts(market: dict[str, Any], decision: float, kickoff: float) -> dict[str, Any] | None:
    bar = _last_within(market["bars"], decision - DECISION_WINDOW_S, decision)
    if bar is None:
        return None
    _t, mid, ask, liq = bar
    depth = _last_within(market["depth"], decision - DEPTH_WINDOW_S, decision)
    close = market["closing_price"]
    if close is None:
        before = [b for b in market["bars"] if b[0] < kickoff - EPS]
        close = (before or market["bars"])[-1][1]
    return {"market_id": market["market_id"], "mid": mid, "ask": ask if ask is not None and 0.0 < ask < 1.0 else None,
            "liq": liq, "levels": None if depth is None else depth[1], "close": close}


def p_market_of(facts: dict[str, Any]) -> float:
    """The devigged home probability from the sides' mids (one side: its own mid)."""
    sides = facts["sides"]
    home = sides.get("home", {}).get("mid")
    away = sides.get("away", {}).get("mid")
    if home is not None and away is not None and home + away > 0:
        return home / (home + away)
    if home is not None:
        return float(home)
    return 1.0 - float(away)


def quote(ask: float, rule: BetRule) -> tuple[float, float, float]:
    """(price, fee, cost) of buying one contract at the ask plus the rule's price_bump."""
    price = ask + rule.price_bump
    fee = rule.taker_rate * price * (1.0 - price)
    return price, fee, price + fee


def lean_of(p_model: float, facts: dict[str, Any], rule: BetRule) -> dict[str, Any] | None:
    """The buyable side with the larger edge at its ask (home wins ties), or None."""
    best: dict[str, Any] | None = None
    for side in SIDES:
        fact = facts["sides"].get(side)
        if not fact or fact.get("ask") is None:
            continue
        p_side = p_model if side == "home" else 1.0 - p_model
        price, fee, cost = quote(float(fact["ask"]), rule)
        if best is None or p_side - cost > best["edge"]:
            best = {"side": side, "p_model": p_side, "price": price, "fee": fee, "cost": cost, "edge": p_side - cost}
    return best


def fill(fact: dict[str, Any], size: int, rule: BetRule) -> tuple[list[tuple[float, int]], str]:
    """([(price, contracts)], "depth" | "touch"): the replayed fill of `size` contracts
    on one side, prices before the rule's price_bump."""
    ask = float(fact["ask"])
    if fact.get("levels") is not None:
        walked = book.walk(fact["levels"], ask, size, rule.participation)
        return [(float(f["price"]), int(f["size"])) for f in walked], "depth"
    liq = fact.get("liq")
    cap = int(math.floor(rule.participation * float(liq) / (100.0 * ask) + EPS)) if liq else 0
    n = min(int(size), cap)
    return ([(ask, n)] if n > 0 else []), "touch"


def plan_replay_bet(p_model: float, facts: dict[str, Any], rule: BetRule) -> dict[str, Any] | None:
    """The one replayed bet (or None) of a game: {"side", "p_model", "price" (average
    entry), "fee" (per contract), "cost", "edge", "stake_cents", "contracts", "clv",
    "fill", "market_id"}."""
    best = lean_of(p_model, facts, rule)
    if best is None or best["edge"] < rule.min_edge:
        return None
    stake = stake_cents(best["edge"], best["cost"], rule)
    size = int(math.floor(stake / (best["cost"] * 100.0) + EPS)) if stake > 0 else 0
    if size <= 0:
        return None
    fact = facts["sides"][best["side"]]
    fills, how = fill(fact, size, rule)
    filled = sum(n for _, n in fills)
    if filled <= 0:
        return None
    notional = math.fsum((price + rule.price_bump) * n for price, n in fills)
    fee = math.fsum(rule.taker_rate * (price + rule.price_bump) * (1.0 - price - rule.price_bump) * n for price, n in fills)
    cents = round_cents((notional + fee) * 100.0)
    if cents <= 0:
        return None
    entry = notional / filled
    cost = (notional + fee) / filled
    close = fact.get("close")
    return {"side": best["side"], "p_model": best["p_model"], "price": entry, "fee": fee / filled, "cost": cost,
            "edge": best["p_model"] - cost, "stake_cents": cents, "contracts": filled,
            "clv": (float(close) - entry) if close is not None else 0.0, "fill": how, "market_id": fact["market_id"]}
