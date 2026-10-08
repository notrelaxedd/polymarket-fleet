"""Stock order approval (contract section 5): one test per reject code, in the contract's
order, plus the reservation arithmetic, idempotency and the one-decision-per-session mark."""
from __future__ import annotations

from datetime import timedelta

import pytest

from host.stocks import approve
from tests.stock_host_helpers import (
    add_bars, assignment_row, db_now, events, one, order, order_row, request, set_broker,
    set_order_status, set_position, set_setting, stock_setup,
)


def _live_assignment(conn, model_status: str = "live_eligible"):
    """A live assignment on live keys (created with live on), then live left as the test sets it."""
    return stock_setup(conn, mode="live", model_status=model_status)


def test_approved_buy_reserves_ceil_of_band(conn):
    s = stock_setup(conn)
    got = one(conn, s, qty=3, ref=999_999)  # the worker's ref price is ignored: the host uses the bar close
    assert got["status"] == "approved" and got["reason"] is None
    row = order_row(conn, got["order_id"])
    # host ref = 100.00 -> 10000 cents; 3 * 10000 * 1.05 = 31500 exactly
    assert row["ref_price_cents"] == 10_000 and row["reserved_cents"] == 31_500 and row["status"] == "approved"
    a = assignment_row(conn, s.assignment["id"])
    assert a["cash_cents"] == 1_000_000 - 31_500 and a["reserved_cents"] == 31_500
    ev = events(conn, got["order_id"])
    assert ev[0]["to_status"] == "approved" and ev[0]["actor"] == "host"
    assert ev[0]["detail"]["worker_ref_price_cents"] == 999_999 and ev[0]["detail"]["reserved_cents"] == 31_500


def test_reservation_rounds_up_to_the_cent():
    assert approve.reservation_cents(1, 10_001, 0.05) == 10_502  # 10501.05 -> 10502
    assert approve.reservation_cents(7, 333, 0.05) == 2_448  # 2447.55 -> 2448
    assert approve.reservation_cents(2, 1_000, 0.05) == 2_100  # exact: no extra cent
    assert approve.reservation_cents(3, 12_345, 0.015) == 37_591  # 37590.525 -> 37591


def test_sell_reserves_nothing_and_cash_tracks_batch(conn):
    s = stock_setup(conn, bankroll=50_000)
    set_position(conn, s.assignment["id"], "AAPL", 5)
    got = request(conn, s, [order(side="sell", qty=2), order(qty=2), order(symbol="SPY", qty=1)])
    assert [g["status"] for g in got] == ["approved", "approved", "rejected"]
    assert got[2]["reason"] == "cash", "the first buy's reservation already left too little cash"
    assert order_row(conn, got[0]["order_id"])["reserved_cents"] == 0
    a = assignment_row(conn, s.assignment["id"])
    assert a["cash_cents"] == 50_000 - 21_000 and a["reserved_cents"] == 21_000


def test_request_is_idempotent_and_marks_the_decision(conn):
    s = stock_setup(conn)
    first = request(conn, s, [order(crid="same", qty=1)])
    again = request(conn, s, [order(crid="same", qty=5)])
    assert again[0]["order_id"] == first[0]["order_id"] and again[0].get("duplicate") is True
    assert conn.execute("SELECT count(*) AS n FROM stock_orders").fetchone()["n"] == 1
    assert assignment_row(conn, s.assignment["id"])["last_decision_date"] == s.session


def test_all_rejected_still_marks_the_decision(conn):
    s = stock_setup(conn)
    got = request(conn, s, [order(side="sell", qty=1)])
    assert got[0]["status"] == "rejected"
    assert assignment_row(conn, s.assignment["id"])["last_decision_date"] == s.session


def test_reject_kill(conn):
    s = stock_setup(conn)
    set_setting(conn, "kill_switch", True)
    got = one(conn, s)
    assert got == {**got, "status": "rejected", "reason": "kill"}
    row = order_row(conn, got["order_id"])
    assert row["status"] == "rejected" and row["reason"] == "kill" and row["reserved_cents"] == 0
    assert events(conn, got["order_id"])[0]["detail"]["reason"] == "kill"


def test_reject_halted(conn):
    s = stock_setup(conn)
    conn.execute("UPDATE stock_assignments SET status = 'halted' WHERE id = %s", (s.assignment["id"],))
    assert one(conn, s)["reason"] == "halted"


def test_reject_environment(conn):
    s = stock_setup(conn)
    set_broker(conn, environment="live")
    assert one(conn, s)["reason"] == "environment"
    set_broker(conn, keys_present=False)
    assert one(conn, s)["reason"] == "environment"


def test_reject_broker_stale(conn):
    s = stock_setup(conn)
    set_broker(conn, checked_at=db_now(conn) - timedelta(seconds=121))  # 4 * stock_broker_poll_s (30)
    assert one(conn, s)["reason"] == "broker_stale"


def test_reject_live_disabled(conn):
    s = _live_assignment(conn)
    set_setting(conn, "live_enabled", False)
    assert one(conn, s)["reason"] == "live_disabled"


def test_reject_not_live_eligible(conn):
    s = _live_assignment(conn)
    conn.execute("UPDATE stock_models SET status = 'paper_ok' WHERE id = %s", (s.model["id"],))
    assert one(conn, s)["reason"] == "not_live_eligible"


def test_reject_market_closed(conn):
    s = stock_setup(conn)
    set_broker(conn, market_open=False, session_date=s.session)
    assert one(conn, s)["reason"] == "market_closed"


def test_reject_moc_cutoff(conn):
    s = stock_setup(conn)
    set_broker(conn, next_close=db_now(conn) + timedelta(minutes=10), session_date=s.session)
    assert one(conn, s)["reason"] == "moc_cutoff"


def test_reject_session(conn):
    s = stock_setup(conn)
    got = request(conn, s, [order()], session=s.session + timedelta(days=1))
    assert got[0]["reason"] == "session"


def test_reject_symbol(conn):
    s = stock_setup(conn)
    add_bars(conn, "MSFT", [s.session - timedelta(days=1)])
    assert one(conn, s, symbol="MSFT")["reason"] == "symbol", "not one of the assignment's symbols"
    conn.execute("UPDATE instruments SET tradable = false WHERE symbol = 'AAPL'")
    assert one(conn, s)["reason"] == "symbol", "the instrument is no longer tradable"


def test_reject_short(conn):
    s = stock_setup(conn)
    assert one(conn, s, side="sell", qty=1)["reason"] == "short", "nothing held"
    set_position(conn, s.assignment["id"], "AAPL", 10)
    first = one(conn, s, side="sell", qty=8)
    assert first["status"] == "approved"
    set_order_status(conn, first["order_id"], "partial", filled=0)
    assert one(conn, s, side="sell", qty=3)["reason"] == "short", "10 held minus 8 in an open sell"
    assert one(conn, s, side="sell", qty=2)["status"] == "approved"


def test_reject_max_order(conn):
    s = stock_setup(conn)
    assert one(conn, s, qty=11)["reason"] == "max_order"  # 11 * $100 > stock_max_order_cents $1000
    assert one(conn, s, qty=10)["status"] == "approved"


def test_reject_max_position(conn):
    s = stock_setup(conn)
    set_position(conn, s.assignment["id"], "AAPL", 20)
    first = one(conn, s, qty=4)  # 24 * $100 = $2400 <= $2500
    assert first["status"] == "approved"
    assert one(conn, s, qty=2)["reason"] == "max_position", "(20 held + 4 open + 2) * $100 > $2500"
    assert one(conn, s, qty=1)["status"] == "approved"


def test_reject_cash(conn):
    s = stock_setup(conn, bankroll=40_000)
    assert one(conn, s, qty=4)["reason"] == "cash", "4 * $100 * 1.05 = $420 > $400"
    got = one(conn, s, qty=3)
    assert got["status"] == "approved" and order_row(conn, got["order_id"])["reserved_cents"] == 31_500


def test_reject_daily_loss_buys_only(conn):
    s = stock_setup(conn)
    conn.execute("INSERT INTO stock_marks (assignment_id, session_date, equity_cents, positions_cents) VALUES (%s, %s, %s, 0)",
                 (s.assignment["id"], s.session - timedelta(days=1), 1_100_001))
    # previous mark 1,100,001 vs now 1,000,000 cash: a loss of 100,001 > paper limit 100,000
    assert one(conn, s, qty=1)["reason"] == "daily_loss"
    set_position(conn, s.assignment["id"], "AAPL", 1)
    assert one(conn, s, side="sell", qty=1)["status"] == "approved", "a sell reduces risk and is never held back"


def test_daily_loss_counts_positions_at_reference_prices(conn):
    s = stock_setup(conn)
    conn.execute("INSERT INTO stock_marks (assignment_id, session_date, equity_cents, positions_cents) VALUES (%s, %s, %s, 0)",
                 (s.assignment["id"], s.session - timedelta(days=1), 1_150_000))
    set_position(conn, s.assignment["id"], "AAPL", 10)  # 10 * $100 = 100,000 cents of stock
    assert approve.mode_loss_cents(conn, "paper", s.session) == 50_000
    assert one(conn, s, qty=1)["status"] == "approved"


def test_order_of_checks_first_failure_wins(conn):
    s = stock_setup(conn, bankroll=40_000)
    set_setting(conn, "kill_switch", True)
    conn.execute("UPDATE stock_assignments SET status = 'halted' WHERE id = %s", (s.assignment["id"],))
    assert one(conn, s, qty=50)["reason"] == "kill"
    set_setting(conn, "kill_switch", False)
    assert one(conn, s, qty=50)["reason"] == "halted"


@pytest.mark.parametrize("bad", [{"qty": 0}, {"side": "short"}, {"ref_price_cents": -1}, {"client_request_id": ""}])
def test_malformed_orders_are_400(conn, bad):
    from host.errors import BadRequest

    s = stock_setup(conn)
    with pytest.raises(BadRequest):
        request(conn, s, [dict(order(), **bad)])


def test_wrong_job_or_token_is_refused(conn):
    from host.errors import Conflict

    s = stock_setup(conn)
    other = stock_setup(conn, symbols=("SPY",))
    body = s.body([order()])
    body["assignment_id"] = other.assignment["id"]
    worker = conn.execute("SELECT * FROM workers WHERE id = %s", (s.worker.id,)).fetchone()
    with pytest.raises(Conflict):
        with conn.transaction():
            approve.request_orders(conn, worker, body)
    body = s.body([order()])
    body["lease_token"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(Conflict):
        with conn.transaction():
            approve.request_orders(conn, worker, body)


def test_a_replaced_job_cannot_order(conn):
    from host.errors import Conflict

    s = stock_setup(conn)
    conn.execute("UPDATE stock_assignments SET job_id = NULL WHERE id = %s", (s.assignment["id"],))
    with pytest.raises(Conflict, match="no longer"):
        request(conn, s, [order()])
