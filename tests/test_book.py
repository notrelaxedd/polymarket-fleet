"""fleet.sim.book: the order-book walk shared by paper fills and snapshot replay."""
from __future__ import annotations

import pytest

from fleet.sim import book

ASKS = [["0.50", "10"], ["0.52", "20"], ["0.60", "100"]]
BIDS = [["0.48", "10"], ["0.46", "20"], ["0.40", "100"]]


def test_buy_walks_asks_up_to_the_limit_with_participation() -> None:
    fills = book.walk(ASKS, 0.52, 50, 0.5)
    assert fills == [{"level": 0, "price": 0.5, "size": 5}, {"level": 1, "price": 0.52, "size": 10}]


def test_sell_walks_bids_down_to_the_limit() -> None:
    fills = book.walk(BIDS, 0.46, 50, 0.5, side="sell")
    assert fills == [{"level": 0, "price": 0.48, "size": 5}, {"level": 1, "price": 0.46, "size": 10}]


def test_taken_resting_and_size_cap() -> None:
    assert book.walk(ASKS, 0.52, 7, 0.5, taken={0: 3}, resting=True) == [
        {"level": 0, "price": 0.52, "size": 2}, {"level": 1, "price": 0.52, "size": 5}]


def test_bad_levels_are_skipped_and_side_is_checked() -> None:
    assert book.walk([["x", 1], None, ["0.5", "4"]], 0.5, 9, 1.0) == [{"level": 2, "price": 0.5, "size": 4}]
    with pytest.raises(ValueError):
        book.walk(ASKS, 0.5, 1, 1.0, side="short")


def test_depth_through_counts_crossing_levels_on_each_side() -> None:
    assert book.depth_through(ASKS, 0.52) == 30.0
    assert book.depth_through(BIDS, 0.46, "sell") == 30.0
