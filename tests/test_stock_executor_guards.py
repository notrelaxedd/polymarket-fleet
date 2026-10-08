"""Guards of the stock executor and the stock tasks (host/exchange/stock_executor.py,
stock_tasks.py) against the in-memory Alpaca of tests/fake_alpaca.py: an approved row
waits for a fresh broker check and is cancelled past the market-on-close cutoff, an
order opposite to another assignment's active order is cancelled as a wash, rows of
the other mode are never touched with the other keys, a second cancel of an order
pending cancel is not a refusal, and a failed reconciliation reaches the broker row
and the heartbeat."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from host.exchange.alpaca_trading import AlpacaRejected
from host.exchange.main import ExchangeLoop
from host.exchange.stock_executor import StockExecutor
from host.exchange.stock_tasks import StockTasks
from tests.fake_alpaca import NY, FakeAlpaca, ny
from tests.stock_host_helpers import assignment_row, db_now, one, order_row, set_position, stock_setup
from tests.test_stock_exchange import DAY, fake, posts, tick  # noqa: F401 - fake is a fixture


def test_a_failed_reconciliation_reaches_the_broker_row_and_the_heartbeat(conn, pool, fake) -> None:
    stock_setup(conn)
    fake.extra_days = {fake.now.astimezone(NY).date()}
    loop = ExchangeLoop(pool)
    loop.stock_tasks = tasks = StockTasks(pool, clock=lambda: fake.now, client_factory=fake.client)
    fake.force["/v2/positions"] = 500
    result = tasks.run_task("stock_broker", fake.now)
    assert "reconciliation failed" in result["error"]
    assert "reconciliation failed" in conn.execute("SELECT last_error FROM stock_broker_state").fetchone()["last_error"]
    assert loop.last_error is not None and "reconciliation failed" in loop.last_error
    with pool.connection() as c:
        loop.task_heartbeat(c, db_now(conn))
    assert "reconciliation failed" in conn.execute("SELECT last_error FROM exchange_state").fetchone()["last_error"]
    del fake.force["/v2/positions"]
    assert tasks.run_task("stock_broker", fake.now)["mismatches"] == [] and loop.last_error is None


def test_an_approved_row_waits_for_a_fresh_broker_and_is_cancelled_past_the_cutoff(conn, pool, fake) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    oid = one(conn, s, qty=2)["order_id"]
    ex, client, now = StockExecutor(), fake.client(), db_now(conn)
    conn.execute("UPDATE stock_broker_state SET checked_at = %s", (now - timedelta(minutes=10),))
    assert tick(pool, ex, client, now)["submitted"] == 0
    assert order_row(conn, oid)["status"] == "approved" and posts(fake) == 0, "a stale check: the row waits"
    conn.execute("UPDATE stock_broker_state SET checked_at = %s", (now,))
    assert tick(pool, ex, client, now)["submitted"] == 1 and order_row(conn, oid)["status"] == "open"
    # the exchange died after the approval and comes back after the close with a failing clock
    oid = one(conn, s, qty=1)["order_id"]
    conn.execute("UPDATE stock_broker_state SET checked_at = %s, next_close = %s",
                 (now - timedelta(hours=4), now - timedelta(hours=3, minutes=30)))
    tick(pool, ex, client, now)
    row = order_row(conn, oid)
    assert (row["status"], row["reason"]) == ("cancelled", "past the market-on-close cutoff (15:50)") and posts(fake) == 1
    assert assignment_row(conn, s.assignment["id"])["reserved_cents"] == order_row(conn, fake_open(conn, s))["reserved_cents"]


def fake_open(conn, s) -> Any:
    return conn.execute("SELECT id FROM stock_orders WHERE assignment_id = %s AND status = 'open'",
                        (s.assignment["id"],)).fetchone()["id"]


def test_an_order_opposite_to_another_assignments_active_order_is_cancelled_as_a_wash(conn, pool, fake) -> None:
    s1 = stock_setup(conn, bankroll=1_000_000)
    set_position(conn, s1.assignment["id"], "AAPL", 10)
    s2 = stock_setup(conn, bankroll=1_000_000)
    sell = one(conn, s1, symbol="AAPL", side="sell", qty=3)["order_id"]
    buy = one(conn, s2, symbol="AAPL", qty=2)["order_id"]
    assert tick(pool, StockExecutor(), fake.client(), db_now(conn))["submitted"] == 1
    assert order_row(conn, sell)["status"] == "open" and posts(fake) == 1
    row = order_row(conn, buy)
    assert row["status"] == "cancelled" and row["reason"].startswith("wash: a sell order of assignment")
    assert (assignment_row(conn, s2.assignment["id"])["cash_cents"], row["reserved_cents"]) == (1_000_000, 0)
    fake.place_direct("SPY", 1, side="sell")
    with pytest.raises(AlpacaRejected, match="wash trade"):
        fake.client().place({"id": "x1", "symbol": "SPY", "qty": 1, "side": "buy"})


def test_rows_of_the_other_mode_are_not_touched_with_the_other_keys(conn, pool, fake) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    ex, paper, now = StockExecutor(), fake.client(), db_now(conn)
    opened = one(conn, s, qty=2)["order_id"]
    tick(pool, ex, paper, now)
    fake.timeout_next = "after"
    pending = one(conn, s, symbol="SPY", qty=1, ref=40_000)["order_id"]
    tick(pool, ex, paper, now)
    assert (order_row(conn, opened)["status"], order_row(conn, pending)["status"]) == ("open", "submitting")
    live = FakeAlpaca(now=fake.now, environment="live")
    tick(pool, ex, live.client(), now + timedelta(seconds=61))
    conn.execute("UPDATE stock_orders SET status = 'cancel_requested' WHERE id = %s", (order_row(conn, opened)["id"],))
    tick(pool, ex, live.client(), now + timedelta(seconds=62))
    assert live.calls == [], "no lookup, cancel or poll of paper rows on the live account"
    assert (order_row(conn, opened)["status"], order_row(conn, pending)["status"]) == ("cancel_requested", "submitting")
    conn.execute("UPDATE stock_orders SET status = 'open' WHERE id = %s", (order_row(conn, opened)["id"],))
    tick(pool, ex, paper, now + timedelta(seconds=63))
    assert order_row(conn, pending)["status"] == "open", "reconciled once the paper keys are back"
    fake.now = ny(DAY, 16)
    fake.run_close()
    tick(pool, ex, paper, now + timedelta(seconds=64))
    assert {order_row(conn, o)["status"] for o in (opened, pending)} == {"filled"}
    assert assignment_row(conn, s.assignment["id"])["reserved_cents"] == 0


def test_a_second_cancel_of_an_order_pending_cancel_is_not_a_refusal(conn, pool, fake) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    oid = one(conn, s, qty=3)["order_id"]
    ex, client, now = StockExecutor(), fake.client(), db_now(conn)
    tick(pool, ex, client, now)
    remote = fake.by_client(oid)
    remote["status"] = "pending_cancel"
    conn.execute("UPDATE stock_orders SET status = 'cancel_requested' WHERE id = %s", (order_row(conn, oid)["id"],))
    tick(pool, ex, client, now)
    row = order_row(conn, oid)
    assert (row["status"], row["reason"]) == ("cancel_requested", None), "Alpaca's 422 for an order pending cancel"
    remote["status"] = "canceled"
    tick(pool, ex, client, now + timedelta(seconds=31))
    assert order_row(conn, oid)["status"] == "cancelled"
