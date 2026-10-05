"""The replay never reads a price recorded after the decision time (docs/ROBUSTNESS.md
B1): a feed bar is labelled with the start of its minute and holds the minute's last
snapshot, so only a bar whose minute closed by the decision time is used, and with a
recorded book the walk limit is that book's own best ask."""
from __future__ import annotations

from typing import Any

import pytest

from fleet.sim.fills import BetRule
from fleet.sim.prices import Replay, plan_replay_bet

PLATFORM = "polymarket_us"
RULE = BetRule(0.05, 0.01, 0.03, 0.25, 100000, 100000, participation=0.5)
GAME = {"game_id": "g1", "kickoff_at": "2025-01-06T00:20:00Z"}  # decision 23:20:00Z at 60 minutes


def _market(bars: list[list[Any]], depth: list[list[Any]] | None = None, close: float | None = 0.70,
            side: str = "home") -> dict[str, Any]:
    return {"market_id": f"m-{side}", "game_id": "g1", "side": side, "platform": PLATFORM, "confirmed": True,
            "closing_price": close, "kickoff_at": GAME["kickoff_at"], "bars": bars, "depth": depth or []}


def _fact(market: dict[str, Any]) -> dict[str, Any] | None:
    facts = Replay([market], PLATFORM).facts(GAME)
    return None if facts is None else facts["sides"]["home"]


def _feed(before: float, after: float) -> dict[str, Any]:
    """What the prices feed returns for snapshots at 23:19:20 (ask `before`, with a book)
    and 23:20:20 (ask `after`, 20 s after the decision)."""
    bars = [["2025-01-05T23:19:00Z", before - 0.02, before, before - 0.01, 500000],
            ["2025-01-05T23:20:00Z", after - 0.02, after, after - 0.01, 500000]]
    depth = [["2025-01-05T23:19:20Z", [[before - 0.02, 80]], [[before, 80], [before + 0.01, 50]]]]
    return _market(bars, depth)


@pytest.mark.parametrize(("before", "after"), [(0.50, 0.60), (0.60, 0.50)])
def test_a_snapshot_in_the_decision_minute_after_the_decision_never_reaches_the_fact(
        before: float, after: float) -> None:
    fact = _fact(_feed(before, after))
    assert fact is not None
    assert fact["ask"] == pytest.approx(before) and fact["mid"] == pytest.approx(before - 0.01)
    assert fact["levels"] == [[before, 80.0], [before + 0.01, 50.0]]
    bet = plan_replay_bet(0.80, {"game_id": "g1", "sides": {"home": fact}}, RULE)
    assert bet is not None, "the price moved after the decision, so the bet is the same either way"
    assert bet["fill"] == "depth" and bet["price"] == pytest.approx(before)
    assert bet["clv"] == pytest.approx(0.70 - before)


def test_rising_and_falling_prices_after_the_decision_replay_alike() -> None:
    up = plan_replay_bet(0.80, {"game_id": "g1", "sides": {"home": _fact(_feed(0.50, 0.60))}}, RULE)
    down = plan_replay_bet(0.80, {"game_id": "g1", "sides": {"home": _fact(_feed(0.50, 0.40))}}, RULE)
    assert up == down


def test_a_bar_only_in_the_decision_minute_or_later_scores_nothing() -> None:
    assert _fact(_market([["2025-01-05T23:20:00Z", 0.48, 0.50, 0.49, 500000]])) is None
    assert _fact(_market([["2025-01-05T23:21:00Z", 0.48, 0.50, 0.49, 500000]])) is None
    oldest = _fact(_market([["2025-01-05T22:49:00Z", 0.48, 0.50, 0.49, 500000]]))
    assert oldest is not None, "a bar labelled 22:49 closed at 22:50, 30 minutes before the decision"
    assert _fact(_market([["2025-01-05T22:48:00Z", 0.48, 0.50, 0.49, 500000]])) is None


def test_the_walk_limit_is_the_book_s_own_best_ask() -> None:
    bars = [["2025-01-05T23:19:00Z", 0.53, 0.55, 0.54, 500000]]
    stale_book = [["2025-01-05T23:18:30Z", [[0.48, 80]], [[0.49, 0], [0.50, 80], [0.52, 50]]]]
    fact = _fact(_market(bars, stale_book))
    assert fact is not None and fact["ask"] == pytest.approx(0.50), "the first level with size is the best ask"
    bet = plan_replay_bet(0.80, {"game_id": "g1", "sides": {"home": fact}}, RULE)
    assert bet is not None and bet["price"] == pytest.approx(0.50), "levels above the book's best ask stay unwalked"
    empty = _fact(_market(bars, [["2025-01-05T23:19:30Z", [], []]]))
    assert empty is not None and empty["ask"] == pytest.approx(0.55) and empty["levels"] == []


def test_the_closing_fallback_ignores_the_bar_of_the_kickoff_minute() -> None:
    bars = [["2025-01-05T23:19:00Z", 0.48, 0.50, 0.49, 500000],
            ["2025-01-06T00:19:00Z", 0.60, 0.62, 0.61, 500000],
            ["2025-01-06T00:20:00Z", 0.80, 0.82, 0.81, 500000]]
    fact = _fact(_market(bars, close=None))
    assert fact is not None and fact["close"] == pytest.approx(0.61)
