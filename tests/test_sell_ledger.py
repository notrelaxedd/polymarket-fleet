"""Selling (docs/TRADING.md "Selling (step 6 Part B)"): the average-cost basis maths by
hand, the ledger `sell` row, signed positions, the no-shorting guard and the ledger
invariants after partial and full sales, plus the P&L of a partly sold position."""
from __future__ import annotations

from datetime import timedelta

import pytest

from host import pnl
from host.errors import Conflict
from host.trading import ledger, orders, positions
from tests.sell_helpers import bought, ledger_rows, sell_order, sold
from tests.test_exchange import NOW, bankroll, make_assignment, make_game, make_market, make_model, snap


def _setup(conn, bankroll_cents: int = 10_000):
    make_game(conn)
    market = make_market(conn, "2026_05_KC_LV", "home")
    assignment = make_assignment(conn, "2026_05_KC_LV", make_model(conn), bankroll_cents=bankroll_cents)
    return market, assignment


def test_sell_basis_maths_by_hand():
    assert positions.sell_basis_cents(10, 520, 4) == 208, "520 * 4 / 10"
    assert positions.sell_basis_cents(3, 157, 1) == 52, "52.33 rounds down"
    assert positions.sell_basis_cents(3, 157, 2) == 105, "104.67 rounds up"
    assert positions.sell_basis_cents(2, 105, 1) == 53, "52.5 rounds half up"
    assert positions.sell_basis_cents(3, 157, 3) == 157, "a closing sale takes exactly the remaining basis"
    assert positions.sell_basis_cents(3, 157, 5) == 157, "never more than the remaining basis"
    assert positions.sell_basis_cents(0, 0, 1) == 0 and positions.sell_basis_cents(5, 100, 0) == 0


def test_ledger_sell_row_and_refusals(conn):
    _, assignment = _setup(conn)
    bank = assignment["bankroll"]
    with pytest.raises(ledger.LedgerError):
        ledger.sell(conn, bank["id"], -1, 100, 0, None)
    with pytest.raises(ledger.LedgerError):
        ledger.sell(conn, bank["id"], 10, 100, 0, None)  # no open cost to remove yet
    assert ledger.replay_problems(conn) == []


def test_partial_then_full_sale_moves_money_by_the_conventions(conn):
    market, assignment = _setup(conn)
    buy = bought(conn, assignment, market, 0.52, 10, 12)
    assert conn.execute("SELECT basis_cents FROM fills WHERE order_id = %s", (buy["id"],)).fetchone()["basis_cents"] == 520
    before = bankroll(conn, assignment)
    assert before["open_cost_cents"] == 520 and before["reserved_cents"] == 0

    first = sold(conn, assignment, market, 0.60, 4, 5)
    assert first["status"] == "filled" and first["filled_size"] == 4 and float(first["avg_fill_price"]) == 0.6
    row = ledger_rows(conn, first["id"])
    assert len(row) == 1 and row[0]["kind"] == "sell"
    assert (row[0]["d_open"], row[0]["d_available"], row[0]["d_reserved"], row[0]["d_realized"]) == (-208, 240 - 5, 0, 240 - 5 - 208)
    fill = conn.execute("SELECT basis_cents, price, size FROM fills WHERE order_id = %s", (first["id"],)).fetchone()
    assert fill["basis_cents"] == 208
    after = bankroll(conn, assignment)
    assert after["open_cost_cents"] == 312 and after["available_cents"] == before["available_cents"] + 235
    assert after["realized_pnl_cents"] == before["realized_pnl_cents"] + 27
    assert ledger.replay_problems(conn) == []
    assert positions.positions(conn, assignment["id"]) == [
        {"market_id": market["id"], "side": "home", "size": 6, "basis_cents": 312, "avg_cost": 0.52}
    ]

    last = sold(conn, assignment, market, 0.45, 6, 7)
    row = ledger_rows(conn, last["id"], "sell")[0]
    assert (row["d_open"], row["d_available"], row["d_realized"]) == (-312, 270 - 7, 270 - 7 - 312)
    final = bankroll(conn, assignment)
    assert final["open_cost_cents"] == 0 and final["reserved_cents"] == 0
    assert final["realized_pnl_cents"] == -12 + 27 + (263 - 312)
    assert final["initial_cents"] + final["realized_pnl_cents"] == final["available_cents"]
    assert positions.positions(conn, assignment["id"]) == [], "a full sale closes the position"
    assert ledger.replay_problems(conn) == []


def test_uneven_basis_sold_in_steps_ends_exactly_at_zero(conn):
    market, assignment = _setup(conn)
    bought(conn, assignment, market, 0.52, 3, 4)
    bought(conn, assignment, market, 0.55, 2, 3)
    assert positions.held(conn, assignment["id"], market["id"]) == (5, 156 + 110)
    removed = []
    for size in (2, 1, 2):
        o = sold(conn, assignment, market, 0.58, size, 1)
        removed.append(conn.execute("SELECT basis_cents FROM fills WHERE order_id = %s", (o["id"],)).fetchone()["basis_cents"])
        assert ledger.replay_problems(conn) == []
    assert removed == [106, 53, 107], "266*2/5=106.4, 160/3=53.3, then the exact 107 left"
    assert sum(removed) == 266 and bankroll(conn, assignment)["open_cost_cents"] == 0
    assert positions.held(conn, assignment["id"], market["id"]) == (0, 0)


def test_no_shorting_and_no_reservation_touched(conn):
    market, assignment = _setup(conn)
    bought(conn, assignment, market, 0.50, 3, 4)
    o = sell_order(conn, assignment, market, 0.55, 5)
    with pytest.raises(Conflict):
        orders.record_fill(conn, o["id"], 0.55, 4, 1, "paper", "test")
    orders.record_fill(conn, o["id"], 0.55, 3, 1, "paper", "test")
    assert orders.remaining_reservation_cents(conn, orders.get_order(conn, o["id"])) == 0
    assert orders.cancel_order(conn, o["id"], "test", "owner") == "cancelled"
    kinds = [r["kind"] for r in ledger_rows(conn, o["id"])]
    assert kinds == ["sell"], "a sell reserves nothing and releases nothing"
    other = make_market(conn, "2026_05_KC_LV", "away")
    stray = sell_order(conn, assignment, other, 0.40, 1)
    with pytest.raises(Conflict):
        orders.record_fill(conn, stray["id"], 0.40, 1, 0, "paper", "test")  # nothing held on that market
    assert ledger.replay_problems(conn) == []


def test_sell_fill_is_idempotent_on_its_fill_id(conn):
    market, assignment = _setup(conn)
    bought(conn, assignment, market, 0.50, 4, 5)
    o = sell_order(conn, assignment, market, 0.55, 4)
    orders.record_fill(conn, o["id"], 0.55, 2, 1, "paper", "test", exchange_fill_id="x:1")
    orders.record_fill(conn, o["id"], 0.55, 2, 1, "paper", "test", exchange_fill_id="x:1")
    assert orders.get_order(conn, o["id"])["filled_size"] == 2 and len(ledger_rows(conn, o["id"], "sell")) == 1
    event = conn.execute("SELECT detail FROM order_events WHERE order_id = %s AND to_status = 'partial'", (o["id"],)).fetchone()
    assert event["detail"]["fill"] == {"price": 0.55, "size": 2, "fee_cents": 1, "side": "sell", "basis_cents": 100, "realized_cents": 9}


def test_pnl_counts_the_sale_and_the_contracts_still_held(conn):
    market, assignment = _setup(conn)
    snap(conn, market["id"], 0.50, 0.52, NOW - timedelta(minutes=10))
    bought(conn, assignment, market, 0.50, 10, 12)
    sold(conn, assignment, market, 0.60, 4, 5)
    snap(conn, market["id"], 0.64, 0.66, NOW - timedelta(minutes=1))
    out = pnl.pnl(conn)["by_mode"]["paper"]
    held_unrealized = 6 * 65 - 300
    realized_sale = 240 - 5 - 200
    assert out["all_time_cents"] == held_unrealized + realized_sale
    assert out["today_cents"] == held_unrealized + realized_sale, "both fills are from today"
    marked = positions.mark_positions(conn, positions.positions(conn, assignment["id"]))
    assert marked[0]["size"] == 6 and marked[0]["unrealized_cents"] == held_unrealized
    assert positions.unrealized_cents(conn, "paper") == held_unrealized
