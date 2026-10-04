"""Paper fill simulation: partial fills across snapshots, the participation cap,
resting bids that fill only when the ask crosses, fee maths and the ledger invariants
after every fill."""
from __future__ import annotations

from datetime import timedelta

from psycopg.types.json import Jsonb

from host.exchange import paper
from host.trading import ledger, orders
from tests.test_exchange import NOW, bankroll, make_assignment, make_game, make_market, make_model, make_order, order, snap

FEE = {"taker_rate": 0.05, "half_spread": 0.01}


def _setup(conn, bankroll_cents: int = 100_000):
    make_game(conn)
    market = make_market(conn, "2026_05_KC_LV", "home")
    assignment = make_assignment(conn, "2026_05_KC_LV", make_model(conn), bankroll_cents=bankroll_cents)
    return market, assignment


def _fills(conn, order_id):
    return conn.execute("SELECT price, size, fee_cents, snapshot_id, exchange_fill_id FROM fills WHERE order_id = %s ORDER BY id", (order_id,)).fetchall()


def test_fee_maths():
    assert paper.fee_per_contract(0.5, FEE) == 0.05 * 0.5 * 0.5
    assert paper.fee_cents(0.5, 100, FEE) == 125, "0.0125 per contract, 100 contracts, in cents"
    assert paper.fee_cents(0.9, 20, FEE) == 9, "0.05 * 0.9 * 0.1 * 20 contracts"
    assert paper.fee_cents(0.5, 100, None) == 125, "defaults when the fee model is missing"
    assert paper.fee_cents(0.5, 100, {"taker_rate": 0.0}) == 0
    assert paper.cost_cents(0.52, 7) == 364


def test_simulate_walks_ask_levels_at_or_below_the_price_with_participation():
    order_row = {"price": 0.55, "size": 100, "filled_size": 0}
    snapshot = {"ask_depth": [[0.52, 40], [0.54, 100], [0.55, 10], [0.56, 1000]]}
    fills = paper.simulate(order_row, snapshot, 0.5, FEE)
    assert [(f["price"], f["size"]) for f in fills] == [(0.52, 20), (0.54, 50), (0.55, 5)], "half of each level up to the price"
    assert fills[0]["fee_cents"] == paper.fee_cents(0.52, 20, FEE) and fills[0]["level"] == 0
    assert [f["size"] for f in paper.simulate(order_row, snapshot, 1.0, FEE)] == [40, 60], "full participation stops at the order size"
    assert [f["size"] for f in paper.simulate(order_row, snapshot, 1.0, FEE, remaining=45)] == [40, 5]
    assert paper.simulate({"price": 0.50, "size": 10, "filled_size": 0}, snapshot, 0.5, FEE) == [], "resting below the ask"
    assert paper.simulate(order_row, {"ask_depth": [[0.52, 1]]}, 0.5, FEE) == [], "participation rounds down"
    assert paper.simulate(order_row, {"ask_depth": [["x", 1], [0.52, 10]]}, 0.5, FEE)[0]["size"] == 5, "bad levels skipped"
    assert paper.simulate(order_row, {"ask_depth": None}, 0.5, FEE) == []


def test_partial_fills_across_snapshots_until_filled(conn):
    market, assignment = _setup(conn)
    o = make_order(conn, assignment, market, 0.55, 100, status="open", submitted_at=NOW)
    snap(conn, market["id"], 0.50, 0.60, NOW - timedelta(seconds=1), size=1000)
    first = snap(conn, market["id"], 0.50, 0.54, NOW + timedelta(seconds=2), size=60)
    assert paper.process(conn) == 2, "levels 0.54 and 0.55 fill, 0.56 is above the price"
    row = order(conn, o["id"])
    assert row["status"] == "partial" and row["filled_size"] == 60
    fills = _fills(conn, o["id"])
    assert [(float(f["price"]), f["size"], f["snapshot_id"]) for f in fills] == [(0.54, 30, first["id"]), (0.55, 30, first["id"])]
    assert fills[0]["exchange_fill_id"] == f"paper:{o['id']}:{first['id']}:0"
    assert float(row["avg_fill_price"]) == 0.545
    assert ledger.replay_problems(conn) == []
    bank = bankroll(conn, assignment)
    assert bank["open_cost_cents"] == 30 * 54 + 30 * 55 and bank["realized_pnl_cents"] == -(fills[0]["fee_cents"] + fills[1]["fee_cents"])
    assert paper.process(conn) == 0, "the same snapshot never fills twice"
    second = snap(conn, market["id"], 0.50, 0.53, NOW + timedelta(seconds=4), size=100)
    assert paper.process(conn) == 1, "40 left: level 0.53 gives 50, capped at the remainder"
    row = order(conn, o["id"])
    assert row["status"] == "filled" and row["filled_size"] == 100
    assert _fills(conn, o["id"])[-1]["snapshot_id"] == second["id"] and _fills(conn, o["id"])[-1]["size"] == 40
    bank = bankroll(conn, assignment)
    assert bank["reserved_cents"] == 0, "the surplus reservation went back after the last fill"
    assert bank["open_cost_cents"] == 30 * 54 + 30 * 55 + 40 * 53
    assert bank["initial_cents"] + bank["realized_pnl_cents"] == bank["available_cents"] + bank["open_cost_cents"]
    assert ledger.replay_problems(conn) == []
    snap(conn, market["id"], 0.50, 0.52, NOW + timedelta(seconds=6))
    assert paper.process(conn) == 0, "a filled order is left alone"


def test_participation_cap_per_level_per_snapshot(conn):
    conn.execute("UPDATE settings SET value = %s WHERE key = 'participation'", (Jsonb(0.25),))
    market, assignment = _setup(conn)
    o = make_order(conn, assignment, market, 0.60, 100, status="open", submitted_at=NOW)
    snap(conn, market["id"], 0.50, 0.60, NOW + timedelta(seconds=1), size=40, levels=1)
    assert paper.process(conn) == 1
    assert order(conn, o["id"])["filled_size"] == 10, "a quarter of the 40 on the level"
    snap(conn, market["id"], 0.50, 0.60, NOW + timedelta(seconds=3), size=40, levels=1)
    assert paper.process(conn) == 1 and order(conn, o["id"])["filled_size"] == 20, "each snapshot gives another quarter"
    snap(conn, market["id"], 0.50, 0.60, NOW + timedelta(seconds=5), size=3, levels=1)
    assert paper.process(conn) == 0 and order(conn, o["id"])["filled_size"] == 20, "0.75 contracts round down to nothing"
    assert ledger.replay_problems(conn) == []


def test_resting_bid_fills_only_when_a_later_ask_crosses(conn):
    market, assignment = _setup(conn)
    o = make_order(conn, assignment, market, 0.45, 20, status="open", submitted_at=NOW)
    snap(conn, market["id"], 0.48, 0.50, NOW + timedelta(seconds=1))
    snap(conn, market["id"], 0.45, 0.47, NOW + timedelta(seconds=3))
    assert paper.process(conn) == 0 and order(conn, o["id"])["status"] == "open"
    crossing = snap(conn, market["id"], 0.42, 0.44, NOW + timedelta(seconds=5), size=100)
    assert paper.process(conn) == 1
    row = order(conn, o["id"])
    assert row["status"] == "filled" and float(row["avg_fill_price"]) == 0.45, "a resting bid fills at its own limit, not the crossing ask"
    assert _fills(conn, o["id"])[0]["snapshot_id"] == crossing["id"]
    bank = bankroll(conn, assignment)
    assert bank["open_cost_cents"] == 20 * 45 and bank["reserved_cents"] == 0
    assert ledger.replay_problems(conn) == []


def test_snapshots_before_submission_and_other_markets_are_ignored(conn):
    market, assignment = _setup(conn)
    other = make_market(conn, "2026_05_KC_LV", "away")
    o = make_order(conn, assignment, market, 0.60, 10, status="open", submitted_at=NOW)
    snap(conn, market["id"], 0.50, 0.52, NOW - timedelta(seconds=5))
    snap(conn, market["id"], 0.50, 0.52, NOW)
    snap(conn, other["id"], 0.50, 0.52, NOW + timedelta(seconds=5))
    assert paper.process(conn) == 0 and order(conn, o["id"])["filled_size"] == 0
    approved = make_order(conn, assignment, market, 0.60, 10)
    snap(conn, market["id"], 0.50, 0.52, NOW + timedelta(seconds=6))
    assert paper.process(conn) == 1
    assert order(conn, approved["id"])["status"] == "approved", "only open and partial orders fill"
    assert order(conn, o["id"])["status"] == "filled"


def test_cancel_requested_and_cancelled_orders_do_not_fill(conn):
    market, assignment = _setup(conn)
    o = make_order(conn, assignment, market, 0.60, 10, status="open", submitted_at=NOW)
    orders.cancel_order(conn, o["id"], "worker", "edge gone")
    snap(conn, market["id"], 0.50, 0.52, NOW + timedelta(seconds=1))
    assert paper.process(conn) == 0 and order(conn, o["id"])["status"] == "cancelled"
    assert bankroll(conn, assignment)["reserved_cents"] == 0 and ledger.replay_problems(conn) == []


def test_fills_never_exceed_the_reservation(conn):
    market, assignment = _setup(conn)
    o = make_order(conn, assignment, market, 0.60, 10, status="open", submitted_at=NOW)
    # The reservation covers 10 at 0.60 plus the fee; a book that is better than the
    # limit costs less, so everything fits. Shrink the reservation by hand to show the cap.
    conn.execute("UPDATE orders SET cost_cents = 300 WHERE id = %s", (o["id"],))
    snap(conn, market["id"], 0.50, 0.55, NOW + timedelta(seconds=1), size=100)
    assert paper.process(conn) == 1
    row = order(conn, o["id"])
    assert row["filled_size"] == 5 and row["status"] == "partial", "5 contracts at 0.55 plus fee fit in 300 cents, 6 do not"
    assert ledger.replay_problems(conn) == []
    snap(conn, market["id"], 0.50, 0.55, NOW + timedelta(seconds=3), size=100)
    assert paper.process(conn) == 0, "nothing left to spend"
    assert ledger.replay_problems(conn) == []


def test_two_orders_two_assignments_fill_independently(conn):
    market, a1 = _setup(conn)
    a2 = make_assignment(conn, "2026_05_KC_LV", make_model(conn), bankroll_cents=5_000)
    o1 = make_order(conn, a1, market, 0.55, 20, status="open", submitted_at=NOW)
    o2 = make_order(conn, a2, market, 0.55, 20, status="open", submitted_at=NOW + timedelta(seconds=2))
    snap(conn, market["id"], 0.50, 0.54, NOW + timedelta(seconds=1), size=100)
    snap(conn, market["id"], 0.50, 0.54, NOW + timedelta(seconds=3), size=100)
    assert paper.process(conn) == 2
    assert order(conn, o1["id"])["filled_size"] == 20 and order(conn, o2["id"])["filled_size"] == 20
    assert _fills(conn, o1["id"])[0]["snapshot_id"] < _fills(conn, o2["id"])[0]["snapshot_id"]
    assert ledger.replay_problems(conn) == []
    assert bankroll(conn, a1)["open_cost_cents"] == bankroll(conn, a2)["open_cost_cents"] == 20 * 54
