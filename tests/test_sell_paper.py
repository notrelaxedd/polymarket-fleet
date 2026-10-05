"""Paper sell fills (docs/TRADING.md "Selling (step 6 Part B)"): a sell walks the bid
levels at or above its limit with the participation rule, rests until a later bid
crosses, shares each bid level with the other paper sells on the market (ask levels
stay the buys'), never sells more than the assignment holds, and keeps the ledger
invariants."""
from __future__ import annotations

from datetime import timedelta

from host.exchange import paper
from host.trading import ledger, orders, positions
from tests.sell_helpers import FEE, bought, ledger_rows, sell_order
from tests.test_exchange import NOW, bankroll, make_assignment, make_game, make_market, make_model, make_order, snap

GAME = "2026_05_KC_LV"


def _setup(conn, holds: int = 100, price: float = 0.50):
    make_game(conn)
    market = make_market(conn, GAME, "home")
    assignment = make_assignment(conn, GAME, make_model(conn), bankroll_cents=100_000)
    if holds:
        bought(conn, assignment, market, price, holds, 0, submitted_at=NOW - timedelta(minutes=5))
    return market, assignment


def _fills(conn, order_id):
    return conn.execute(
        "SELECT price, size, fee_cents, basis_cents, snapshot_id, exchange_fill_id FROM fills WHERE order_id = %s ORDER BY id", (order_id,)
    ).fetchall()


def test_simulate_sell_walks_bid_levels_at_or_above_the_limit():
    order_row = {"price": 0.50, "size": 100, "filled_size": 0, "side": "sell"}
    snapshot = {"bid_depth": [[0.53, 40], [0.51, 100], [0.50, 10], [0.49, 1000]], "ask_depth": [[0.40, 1000]]}
    fills = paper.simulate(order_row, snapshot, 0.5, FEE)
    assert [(f["price"], f["size"], f["level"]) for f in fills] == [(0.53, 20, 0), (0.51, 50, 1), (0.50, 5, 2)]
    assert fills[0]["fee_cents"] == paper.fee_cents(0.53, 20, FEE)
    assert paper.simulate({**order_row, "price": 0.54}, snapshot, 0.5, FEE) == [], "the best bid is below the limit"
    resting = paper.simulate(order_row, snapshot, 0.5, FEE, resting=True)
    assert {f["price"] for f in resting} == {0.50}, "a resting sell fills at its own limit"
    assert [f["size"] for f in paper.simulate(order_row, snapshot, 0.5, FEE, taken={0: 15, 1: 50})] == [5, 5]
    assert paper.fill_id("o", 7, 2, "sell") == "paper:o:7:b2" and paper.fill_id("o", 7, 2, "buy") == "paper:o:7:2"


def test_marketable_sell_fills_bid_levels_and_books_the_sale(conn):
    market, assignment = _setup(conn)
    o = sell_order(conn, assignment, market, 0.50, 60, submitted_at=NOW)
    snap(conn, market["id"], 0.40, 0.42, NOW - timedelta(seconds=1), size=1000)
    first = snap(conn, market["id"], 0.52, 0.54, NOW + timedelta(seconds=2), size=60)
    assert paper.process(conn) == 2, "0.52 and 0.51 give 30 each, the order is done"
    row = orders.get_order(conn, o["id"])
    assert row["status"] == "filled" and row["filled_size"] == 60 and float(row["avg_fill_price"]) == 0.515
    fills = _fills(conn, o["id"])
    assert [(float(f["price"]), f["size"], f["basis_cents"]) for f in fills] == [(0.52, 30, 1500), (0.51, 30, 1500)]
    assert [f["exchange_fill_id"] for f in fills] == [f"paper:{o['id']}:{first['id']}:b0", f"paper:{o['id']}:{first['id']}:b1"]
    fee = sum(f["fee_cents"] for f in fills)
    assert fee == paper.fee_increment_cents(0.52, 30, None, 0.0) + paper.fee_increment_cents(0.51, 30, None, paper.fee_per_contract(0.52, None) * 30)
    assert positions.held(conn, assignment["id"], market["id"]) == (40, 2000)
    sells = ledger_rows(conn, o["id"], "sell")
    assert sum(r["d_realized"] for r in sells) == 1560 + 1530 - fee - 3000
    bank = bankroll(conn, assignment)
    assert bank["open_cost_cents"] == 2000 and bank["reserved_cents"] == 0
    assert ledger.replay_problems(conn) == []
    snap(conn, market["id"], 0.52, 0.54, NOW + timedelta(seconds=4), size=60)
    assert paper.process(conn) == 0, "a filled sell is left alone"


def test_resting_sell_fills_at_its_limit_when_a_later_bid_crosses(conn):
    market, assignment = _setup(conn)
    o = sell_order(conn, assignment, market, 0.60, 30, submitted_at=NOW)
    snap(conn, market["id"], 0.55, 0.57, NOW + timedelta(seconds=1), size=100)
    assert paper.process(conn) == 0, "bid 0.55 is below the 0.60 limit: the sell rests"
    crossed = snap(conn, market["id"], 0.62, 0.64, NOW + timedelta(seconds=3), size=20)
    assert paper.process(conn) == 3, "levels 0.62, 0.61, 0.60 cross; 10 contracts each"
    fills = _fills(conn, o["id"])
    assert {float(f["price"]) for f in fills} == {0.60} and sum(f["size"] for f in fills) == 30
    assert all(f["snapshot_id"] == crossed["id"] for f in fills)
    assert orders.get_order(conn, o["id"])["status"] == "filled"
    assert ledger.replay_problems(conn) == []


def test_participation_is_shared_per_level_and_per_side(conn):
    market, first = _setup(conn)
    second = make_assignment(conn, GAME, make_model(conn), bankroll_cents=100_000)
    third = make_assignment(conn, GAME, make_model(conn), bankroll_cents=100_000)
    bought(conn, second, market, 0.50, 100, 0, submitted_at=NOW - timedelta(minutes=5))
    sell_a = sell_order(conn, first, market, 0.50, 100, submitted_at=NOW)
    sell_b = sell_order(conn, second, market, 0.50, 100, submitted_at=NOW + timedelta(milliseconds=10))
    buy = make_order(conn, third, market, 0.60, 100, status="open", submitted_at=NOW + timedelta(milliseconds=20))
    one = snap(conn, market["id"], 0.52, 0.55, NOW + timedelta(seconds=2), size=40)
    paper.process(conn)
    a, b, c = (orders.get_order(conn, x["id"]) for x in (sell_a, sell_b, buy))
    assert a["filled_size"] == 60, "the first sell takes half of each of the three bid levels"
    assert b["filled_size"] == 0, "nothing is left on the bid side of this snapshot for the second sell"
    assert c["filled_size"] == 60, "the buy takes the ask levels, untouched by the sells"
    assert paper.taken_from(conn, one["id"], "sell") == {0: 20, 1: 20, 2: 20}
    assert paper.taken_from(conn, one["id"], "buy") == {0: 20, 1: 20, 2: 20}
    snap(conn, market["id"], 0.52, 0.55, NOW + timedelta(seconds=4), size=40)
    paper.process(conn)
    assert orders.get_order(conn, sell_a["id"])["filled_size"] == 100, "40 left of 60 offered"
    assert orders.get_order(conn, sell_b["id"])["filled_size"] == 20, "the 20 the first sell left"
    assert ledger.replay_problems(conn) == []


def test_sell_never_fills_more_than_the_position(conn):
    market, assignment = _setup(conn, holds=10)
    o = sell_order(conn, assignment, market, 0.50, 25, submitted_at=NOW)
    snap(conn, market["id"], 0.55, 0.57, NOW + timedelta(seconds=1), size=1000)
    paper.process(conn)
    row = orders.get_order(conn, o["id"])
    assert row["filled_size"] == 10 and row["status"] == "partial"
    assert positions.positions(conn, assignment["id"]) == []
    snap(conn, market["id"], 0.55, 0.57, NOW + timedelta(seconds=2), size=1000)
    assert paper.process(conn) == 0, "nothing left to sell"
    assert bankroll(conn, assignment)["open_cost_cents"] == 0
    assert ledger.replay_problems(conn) == []


def test_nothing_fills_under_kill(conn):
    market, assignment = _setup(conn)
    sell_order(conn, assignment, market, 0.50, 10, submitted_at=NOW)
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'kill_switch'")
    snap(conn, market["id"], 0.55, 0.57, NOW + timedelta(seconds=1), size=1000)
    assert paper.process(conn) == 0
