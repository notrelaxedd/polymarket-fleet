"""Settlement after a round trip (docs/TRADING.md "Selling (step 6 Part B)"): a buy
sold out before a re-buy keeps the market outcome as its result but no share of what
is still held (pnl = minus its fee, decision 5), and the lot actually held at
settlement carries the whole remaining basis and payout."""
from __future__ import annotations

from host.exchange import settle, settle_sells
from host.trading import ledger, orders
from tests.sell_helpers import bought, sold
from tests.test_exchange import bankroll, make_order
from tests.test_sell_settlement import GAME, _bets, _final


def test_split_gives_a_zero_weight_nothing_not_the_residual():
    assert settle_sells.split(500, [3, 3, 0]) == [250, 250, 0]
    assert settle_sells.split(100, [1, 2, 0]) == [33, 67, 0]


def test_a_buy_sold_out_before_a_rebuy_gets_no_share_of_the_held_lot(conn):
    home, _, assignment = _final(conn, 27, 17)
    lot_a = bought(conn, assignment, home, 0.40, 10, 12)
    sale = sold(conn, assignment, home, 0.50, 10, 12)
    lot_b = bought(conn, assignment, home, 0.60, 5, 6)
    assert bankroll(conn, assignment)["open_cost_cents"] == 300
    result = settle.settle_game(conn, GAME, "test")
    rows = _bets(conn, assignment)
    a, s, b = rows[lot_a["id"]], rows[sale["id"]], rows[lot_b["id"]]
    assert (a["result"], a["cost_cents"], a["pnl_cents"]) == ("win", 0, -12)
    assert (s["result"], s["cost_cents"], s["pnl_cents"]) == ("sold", 400, 500 - 12 - 400)
    assert (b["result"], b["cost_cents"], b["pnl_cents"]) == ("win", 300, 500 - 300 - 6)
    assert result["pnl_cents"] == -12 + 88 + 194
    assert bankroll(conn, assignment)["realized_pnl_cents"] == -12 + 88 + 194
    assert ledger.replay_problems(conn) == []


def test_a_buy_partly_filled_before_flat_counts_only_its_later_fills(conn):
    home, _, assignment = _final(conn, 27, 17)
    lot_a = make_order(conn, assignment, home, 0.40, 10, status="open")
    orders.record_fill(conn, lot_a["id"], 0.40, 4, 5, "paper", "test")
    sale = sold(conn, assignment, home, 0.50, 4, 5)
    orders.record_fill(conn, lot_a["id"], 0.40, 6, 7, "paper", "test")
    lot_b = bought(conn, assignment, home, 0.50, 4, 5)
    settle.settle_game(conn, GAME, "test")
    rows = _bets(conn, assignment)
    a, b = rows[lot_a["id"]], rows[lot_b["id"]]
    # Held at settlement: A's later 6 (basis 240, payout 600) and B's 4 (basis 200, payout 400).
    assert (a["cost_cents"], a["pnl_cents"]) == (240, 600 - 240 - 12)
    assert (b["cost_cents"], b["pnl_cents"]) == (200, 400 - 200 - 5)
    assert rows[sale["id"]]["pnl_cents"] == 200 - 5 - 160
    assert ledger.replay_problems(conn) == []
