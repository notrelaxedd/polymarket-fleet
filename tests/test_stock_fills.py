"""Booking stock fills (contract section 6, host/exchange/stock_fills.py): the
reservation release arithmetic, cash and positions for buys and sells, realized P&L,
idempotency on exchange_fill_id, and the refusals (a sell larger than the position, a
fill on a closed order): a warning on paper, the auto-kill late_fill on live."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg

from host.exchange.stock_fills import book_fill, money_cents, share
from host.stocks.approve import reservation_cents
from tests.stock_host_helpers import assignment_row, audit, set_position, stock_setup

NOW = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)


def insert_order(conn: psycopg.Connection, a: dict[str, Any], symbol: str = "AAPL", side: str = "buy", qty: int = 10,
                 ref: int = 10_000, status: str = "open", band: str = "0.05", exchange_id: str | None = "ex-1") -> dict[str, Any]:
    """An order row as the approval leaves it (a buy's reservation moved out of cash)."""
    reserved = reservation_cents(qty, ref, band) if side == "buy" else 0
    oid = uuid.uuid4()
    row = conn.execute(
        """
        INSERT INTO stock_orders (id, assignment_id, mode, session_date, client_request_id, symbol, side, qty,
                                  ref_price_cents, reserved_cents, status, exchange_order_id)
        VALUES (%s, %s, %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (oid, a["id"], a["mode"], uuid.uuid4().hex[:24], symbol, side, qty, ref, reserved, status, exchange_id),
    ).fetchone()
    conn.execute("UPDATE stock_assignments SET cash_cents = cash_cents - %s, reserved_cents = reserved_cents + %s WHERE id = %s",
                 (reserved, reserved, a["id"]))
    return dict(row)


def order_of(conn: psycopg.Connection, oid: Any) -> dict[str, Any]:
    return conn.execute("SELECT * FROM stock_orders WHERE id = %s", (oid,)).fetchone()


def position(conn: psycopg.Connection, aid: Any, symbol: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM stock_positions WHERE assignment_id = %s AND symbol = %s", (aid, symbol)).fetchone()


def test_rounding_helpers() -> None:
    assert money_cents(1, 100.005) == 10_001 and money_cents(3, "101.234") == 30_370 and money_cents(7, 99.5) == 69_650
    assert share(105_000, 3, 10) == 31_500 and share(7, 1, 2) == 4 and share(73_500, 7, 7) == 73_500


def test_partial_then_full_buy_releases_the_reservation_exactly(conn) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    o = insert_order(conn, s.assignment, qty=10, ref=10_000)
    assert o["reserved_cents"] == 105_000
    a = assignment_row(conn, s.assignment["id"])
    assert (a["cash_cents"], a["reserved_cents"]) == (895_000, 105_000)
    assert book_fill(conn, o["id"], 3, 101.234, "ex-1:3", NOW) is True
    row, a = order_of(conn, o["id"]), assignment_row(conn, s.assignment["id"])
    assert (row["status"], row["filled_qty"], row["reserved_cents"]) == ("partial", 3, 73_500)
    assert (a["reserved_cents"], a["cash_cents"]) == (73_500, 895_000 + 31_500 - 30_370)
    assert book_fill(conn, o["id"], 7, 99.5, "ex-1:10", NOW) is True
    row, a = order_of(conn, o["id"]), assignment_row(conn, s.assignment["id"])
    assert (row["status"], row["filled_qty"], row["reserved_cents"]) == ("filled", 10, 0)
    assert abs(row["avg_fill_price"] - (3 * 101.234 + 7 * 99.5) / 10) < 1e-9
    assert (a["reserved_cents"], a["cash_cents"]) == (0, 1_000_000 - 30_370 - 69_650)
    pos = position(conn, s.assignment["id"], "AAPL")
    assert (pos["qty"], pos["cost_cents"]) == (10, 100_020)
    statuses = [e["to_status"] for e in conn.execute("SELECT to_status FROM stock_order_events WHERE order_id = %s ORDER BY id",
                                                      (o["id"],)).fetchall()]
    assert statuses == ["partial", "filled"]


def test_the_same_fill_is_booked_once(conn) -> None:
    s = stock_setup(conn)
    o = insert_order(conn, s.assignment, qty=5)
    assert book_fill(conn, o["id"], 2, 100.0, "ex-1:2", NOW) is True
    before = assignment_row(conn, s.assignment["id"])
    assert book_fill(conn, o["id"], 2, 100.0, "ex-1:2", NOW) is False
    assert assignment_row(conn, s.assignment["id"]) == before and order_of(conn, o["id"])["filled_qty"] == 2
    assert conn.execute("SELECT count(*) AS n FROM stock_fills").fetchone()["n"] == 1
    assert not audit(conn, "stock_fill_refused"), "a duplicate is not a refusal"


def test_a_sell_books_proceeds_and_realized_against_the_average_cost(conn) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    set_position(conn, s.assignment["id"], "AAPL", 10, 100_020)
    o = insert_order(conn, s.assignment, side="sell", qty=4)
    assert book_fill(conn, o["id"], 4, 110.0, "ex-1:4", NOW) is True
    a, pos = assignment_row(conn, s.assignment["id"]), position(conn, s.assignment["id"], "AAPL")
    assert (a["cash_cents"], a["realized_cents"], a["reserved_cents"]) == (1_044_000, 44_000 - 40_008, 0)
    assert (pos["qty"], pos["cost_cents"]) == (6, 60_012)
    o2 = insert_order(conn, s.assignment, side="sell", qty=6)
    assert book_fill(conn, o2["id"], 6, 90.0, "ex-2:6", NOW) is True
    pos = position(conn, s.assignment["id"], "AAPL")
    assert (pos["qty"], pos["cost_cents"]) == (0, 0)
    assert assignment_row(conn, s.assignment["id"])["realized_cents"] == 3_992 + 54_000 - 60_012


def test_a_sell_larger_than_the_position_is_refused_with_a_paper_warning(conn) -> None:
    s = stock_setup(conn)
    set_position(conn, s.assignment["id"], "AAPL", 2, 20_000)
    o = insert_order(conn, s.assignment, side="sell", qty=5)
    before = assignment_row(conn, s.assignment["id"])
    assert book_fill(conn, o["id"], 3, 100.0, "ex-1:3", NOW) is False
    assert assignment_row(conn, s.assignment["id"]) == before and position(conn, s.assignment["id"], "AAPL")["qty"] == 2
    assert conn.execute("SELECT count(*) AS n FROM stock_fills").fetchone()["n"] == 0
    warnings = conn.execute("SELECT warnings FROM stock_broker_state").fetchone()["warnings"]
    assert warnings[-1]["kind"] == "fill_refused" and "larger than the position" in warnings[-1]["message"]
    assert audit(conn, "stock_fill_refused") and conn.execute("SELECT value FROM settings WHERE key = 'kill_switch'").fetchone()["value"] is False


def test_a_refused_live_fill_auto_kills_late_fill(conn) -> None:
    s = stock_setup(conn, mode="live")
    o = insert_order(conn, s.assignment, side="sell", qty=1)
    assert book_fill(conn, o["id"], 1, 100.0, "ex-1:1", NOW) is False
    assert conn.execute("SELECT value FROM settings WHERE key = 'kill_switch'").fetchone()["value"] is True
    kills = audit(conn, "auto_kill")
    assert kills and kills[-1]["entity"] == "late_fill"
    assert assignment_row(conn, s.assignment["id"])["status"] == "halted"


def test_a_fill_on_a_closed_or_overfilled_order_is_refused(conn) -> None:
    s = stock_setup(conn)
    o = insert_order(conn, s.assignment, qty=2, status="cancelled")
    assert book_fill(conn, o["id"], 1, 100.0, "ex-1:1", NOW) is False
    o2 = insert_order(conn, s.assignment, qty=2)
    assert book_fill(conn, o2["id"], 3, 100.0, "ex-2:3", NOW) is False
    assert book_fill(conn, uuid.uuid4(), 1, 100.0, "ex-3:1", NOW) is False
    assert len(audit(conn, "stock_fill_refused")) == 2


def test_a_buy_filled_above_its_reservation_is_booked_as_it_is_and_flagged(conn) -> None:
    s = stock_setup(conn, bankroll=105_000)
    o = insert_order(conn, s.assignment, qty=10, ref=10_000)  # reserves 105_000: all the cash
    assert book_fill(conn, o["id"], 10, 108.0, "ex-1:10", NOW) is True  # the close 8% above the previous one
    a, pos = assignment_row(conn, s.assignment["id"]), position(conn, s.assignment["id"], "AAPL")
    assert (a["cash_cents"], a["reserved_cents"], pos["qty"], pos["cost_cents"]) == (-3_000, 0, 10, 108_000)
    (row,) = audit(conn, "stock_fill_over_reservation")
    assert (row["after"]["over_cents"], row["after"]["reserved_part_cents"], row["after"]["cost_cents"]) == (3_000, 105_000, 108_000)
    warnings = conn.execute("SELECT warnings FROM stock_broker_state").fetchone()["warnings"]
    assert warnings[-1]["kind"] == "fill_over_reservation" and "3000 cents above" in warnings[-1]["message"]
    detail = conn.execute("SELECT detail FROM stock_order_events WHERE order_id = %s ORDER BY id DESC LIMIT 1",
                          (o["id"],)).fetchone()["detail"]
    assert detail["over_reservation_cents"] == 3_000
    o2 = insert_order(conn, s.assignment, qty=1, ref=10_000)
    assert book_fill(conn, o2["id"], 1, 100.0, "ex-2:1", NOW) is True
    assert len(audit(conn, "stock_fill_over_reservation")) == 1, "a fill inside the band is not flagged"
