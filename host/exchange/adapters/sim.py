"""Deterministic synthetic markets from the games table (no network).

Two markets per game within the lookahead (home YES, away YES). The mid is the
devigged nflverse moneyline (0.5 when missing) plus a random walk seeded by
`game_id + minute` (steps of 0.005, clamped 0.03..0.97), the spread is 0.02 and each
side shows 5 levels one tick apart with sizes 50..500, so the book is worth thousands
of dollars within 5 cents of the touch.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from fleet.sim.odds import devig
from host.exchange.adapters.base import Book, MarketInfo, MarketSource, SourceError, utcnow

PLATFORM = "sim"
TICK = 0.01
SPREAD = 0.02
LEVELS = 5
STEP = 0.005
WALK_STEPS = 24
MIN_MID, MAX_MID = 0.03, 0.97
SIZE_RANGE = (50, 500)


def market_ref(game_id: str, side: str) -> str:
    return f"sim:{game_id}:{side}"


def parse_ref(ref: str) -> tuple[str, str]:
    """(game_id, side) of a sim market ref; SourceError when it is not one."""
    parts = (ref or "").split(":")
    if len(parts) != 3 or parts[0] != PLATFORM or parts[2] not in ("home", "away"):
        raise SourceError(f"not a sim market ref: {ref!r}")
    return parts[1], parts[2]


def base_home_p(game: dict[str, Any]) -> float:
    p = devig(game.get("home_moneyline"), game.get("away_moneyline"))
    return 0.5 if p is None else float(p)


def home_mid(game: dict[str, Any], now: datetime) -> float:
    """The home YES mid at `now`: base probability plus the minute's seeded walk."""
    minute = int(now.timestamp() // 60)
    rng = random.Random(f"{game['game_id']}{minute}")
    mid = base_home_p(game)
    for _ in range(WALK_STEPS):
        mid += STEP if rng.random() < 0.5 else -STEP
    return min(MAX_MID, max(MIN_MID, mid))


def _round_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 4)


def build_book(game: dict[str, Any], side: str, now: datetime) -> Book:
    mid = home_mid(game, now)
    if side == "away":
        mid = 1.0 - mid
    minute = int(now.timestamp() // 60)
    rng = random.Random(f"{game['game_id']}:{side}:{minute}:sizes")
    bid0 = _round_tick(mid - SPREAD / 2)
    ask0 = _round_tick(mid + SPREAD / 2)
    bids, asks = [], []
    for i in range(LEVELS):
        bp = _round_tick(bid0 - i * TICK)
        ap = _round_tick(ask0 + i * TICK)
        if bp > 0:
            bids.append([bp, float(rng.randint(*SIZE_RANGE))])
        if ap < 1:
            asks.append([ap, float(rng.randint(*SIZE_RANGE))])
    return Book(bids=bids, asks=asks, fetched_at=now)


def within_lookahead(game: dict[str, Any], now: datetime, lookahead_days: int) -> bool:
    kickoff = game.get("kickoff_at")
    if kickoff is None or game.get("status") == "final":
        return False
    return now - timedelta(hours=6) <= kickoff <= now + timedelta(days=lookahead_days)


class SimSource(MarketSource):
    """Synthetic markets; `games` is the games table slice the caller hands over."""

    name = PLATFORM

    def __init__(self, games: list[dict[str, Any]] | None = None, clock: Any = None) -> None:
        self.games: dict[str, dict[str, Any]] = {g["game_id"]: g for g in (games or [])}
        self.clock = clock or utcnow

    def list_markets(self, games: list[dict[str, Any]], lookahead_days: int) -> list[MarketInfo]:
        now = self.clock()
        out: list[MarketInfo] = []
        for game in games:
            self.games[game["game_id"]] = game
            if not within_lookahead(game, now, lookahead_days):
                continue
            for side in ("home", "away"):
                team = game["home_team"] if side == "home" else game["away_team"]
                out.append(MarketInfo(
                    platform=PLATFORM,
                    market_ref=market_ref(game["game_id"], side),
                    event_ref=f"sim:{game['game_id']}",
                    title=f"{team} to win {game['away_team']} @ {game['home_team']}",
                    home_team=game["home_team"],
                    away_team=game["away_team"],
                    side=side,
                    kickoff_at=game["kickoff_at"],
                    tick=TICK,
                    min_size=1,
                    raw={"game_id": game["game_id"], "side": side},
                ))
        return out

    def fetch_book(self, market_ref: str) -> Book:
        game_id, side = parse_ref(market_ref)
        game = self.games.get(game_id)
        if game is None:
            raise SourceError(f"sim market for unknown game {game_id}")
        return build_book(game, side, self.clock())

    def probe(self) -> dict[str, Any]:
        now = self.clock()
        markets = [m.__dict__ for m in self.list_markets(list(self.games.values()), 365)]
        for m in markets:
            m["kickoff_at"] = m["kickoff_at"].isoformat() if m["kickoff_at"] else None
        return {"url": "sim://games", "status": 200, "payload": json.dumps({"generated_at": now.isoformat(), "markets": markets})}


def game_time(value: Any) -> datetime | None:
    """A kickoff value as an aware UTC datetime (rows from the games table already are)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None
