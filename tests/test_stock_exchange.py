"""The stock order outbox of the exchange process (contract section 6,
host/exchange/stock_executor.py and stock_tasks.py) against the in-memory Alpaca of
tests/fake_alpaca.py: submit, timeout and reconcile, definite refusals and 429, the
cancel before and after 15:50, fills booked once, orders closed at Alpaca, the kill,
and that no task touches Alpaca without keys."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from host.exchange import alpaca_credentials as ac
from host.exchange.main import ExchangeLoop, run_once
from host.exchange.stock_executor import REFUSED, StockExecutor
from host.exchange.stock_tasks import NO_KEYS, StockTasks
from host.stocks import assignments
from tests.fake_alpaca import FakeAlpaca, ny
from tests.stock_host_helpers import assignment_row, db_now, one, order, order_row, request, set_position, stock_setup

DAY = date(2027, 3, 4)  # a Thursday, far from the real calendar


@pytest.fixture
def fake() -> FakeAlpaca:
    f = FakeAlpaca(now=ny(DAY, 11))
    f.close_prices = {"AAPL": Decimal("101.5"), "SPY": Decimal("400")}
    return f


def tick(pool, ex: StockExecutor, client: Any, now: datetime, poll: bool = True) -> dict[str, Any]:
    with pool.connection() as c:
        return ex.tick(c, client, now, poll=poll)


def posts(fake: FakeAlpaca) -> int:
    return sum(1 for m, p in fake.calls if m == "POST")


def fills(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM stock_fills").fetchone()["n"]


def test_an_approved_buy_is_placed_as_cls_and_its_fills_are_booked_once(conn, pool, fake) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    oid = one(conn, s, symbol="AAPL", qty=5)["order_id"]
    ex, client, now = StockExecutor(), fake.client(), db_now(conn)
    assert tick(pool, ex, client, now)["submitted"] == 1
    row = order_row(conn, oid)
    remote = fake.by_client(oid)
    assert row["status"] == "open" and row["exchange_order_id"] == remote["id"]
    assert (remote["qty"], remote["side"], remote["type"], remote["time_in_force"]) == ("5", "buy", "market", "cls")
    fake.fill(remote["id"], 2, "101.25")
    tick(pool, ex, client, now)
    tick(pool, ex, client, now)
    row = order_row(conn, oid)
    assert (row["status"], row["filled_qty"], fills(conn)) == ("partial", 2, 1)
    fake.run_close()
    tick(pool, ex, client, now)
    tick(pool, ex, client, now)
    row, a = order_row(conn, oid), assignment_row(conn, s.assignment["id"])
    assert (row["status"], row["filled_qty"], row["reserved_cents"], fills(conn)) == ("filled", 5, 0, 2)
    ids = [r["exchange_fill_id"] for r in conn.execute("SELECT exchange_fill_id FROM stock_fills ORDER BY id").fetchall()]
    assert ids == [f"{remote['id']}:2", f"{remote['id']}:5"]
    assert (a["reserved_cents"], a["cash_cents"]) == (0, 1_000_000 - 20_250 - 30_450)
    assert posts(fake) == 1


def test_sells_are_placed_before_the_buys_of_a_batch(conn, pool, fake) -> None:
    s = stock_setup(conn)
    set_position(conn, s.assignment["id"], "AAPL", 10)
    out = request(conn, s, [order("SPY", "buy", 1, 40_000), order("AAPL", "sell", 3)])
    assert [o["status"] for o in out] == ["approved", "approved"]
    tick(pool, StockExecutor(), fake.client(), db_now(conn))
    assert [o["side"] for o in fake.orders.values()] == ["sell", "buy"]


def test_a_timeout_after_the_order_reached_alpaca_is_reconciled_never_resubmitted(conn, pool, fake) -> None:
    s = stock_setup(conn)
    oid = one(conn, s, qty=2)["order_id"]
    ex, client, now = StockExecutor(), fake.client(), db_now(conn)
    fake.timeout_next = "after"
    tick(pool, ex, client, now)
    assert order_row(conn, oid)["status"] == "submitting" and posts(fake) == 1
    tick(pool, ex, client, now + timedelta(seconds=1))
    assert order_row(conn, oid)["status"] == "submitting", "reconciled only after 5 s"
    tick(pool, ex, client, now + timedelta(seconds=6))
    row = order_row(conn, oid)
    assert row["status"] == "open" and row["exchange_order_id"] == fake.by_client(oid)["id"] and posts(fake) == 1


def test_a_timeout_before_alpaca_saw_it_expires_after_the_grace_with_the_release(conn, pool, fake) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    oid = one(conn, s, qty=2)["order_id"]
    ex, client, now = StockExecutor(), fake.client(), db_now(conn)
    fake.timeout_next = "before"
    tick(pool, ex, client, now)
    tick(pool, ex, client, now + timedelta(seconds=30))
    assert order_row(conn, oid)["status"] == "submitting"
    tick(pool, ex, client, now + timedelta(seconds=61))
    row, a = order_row(conn, oid), assignment_row(conn, s.assignment["id"])
    assert (row["status"], row["reserved_cents"]) == ("expired", 0) and posts(fake) == 1
    assert (a["cash_cents"], a["reserved_cents"]) == (1_000_000, 0)


def test_a_definite_refusal_closes_with_the_release_and_a_429_retries_later(conn, pool, fake) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    fake.close_prices["AAPL"] = Decimal("10000000")
    oid = one(conn, s, qty=2)["order_id"]
    ex, client, now = StockExecutor(), fake.client(), db_now(conn)
    tick(pool, ex, client, now)
    row = order_row(conn, oid)
    assert row["status"] == "rejected_by_exchange" and "insufficient buying power" in row["reason"]
    assert assignment_row(conn, s.assignment["id"])["cash_cents"] == 1_000_000
    fake.close_prices["AAPL"] = Decimal("100")
    oid = one(conn, s, qty=1)["order_id"]
    fake.force["/v2/orders"] = 429
    tick(pool, ex, client, now)
    assert order_row(conn, oid)["status"] == "approved" and client.backoff_remaining() > 0
    del fake.force["/v2/orders"]
    calls = len(fake.calls)
    tick(pool, ex, client, now, poll=False)
    assert len(fake.calls) == calls and order_row(conn, oid)["status"] == "approved", "no request during the backoff"
    client.backoff_until = 0.0
    tick(pool, ex, client, now)
    assert order_row(conn, oid)["status"] == "open"


def test_a_cancel_before_the_cutoff_closes_the_order_with_the_release(conn, pool, fake) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    oid = one(conn, s, qty=3)["order_id"]
    ex, client, now = StockExecutor(), fake.client(), db_now(conn)
    tick(pool, ex, client, now)
    fake.now = ny(DAY, 15, 45)
    with conn.transaction():
        assignments.halt_assignment(conn, s.assignment["id"], "test", "owner@example.com")
    assert order_row(conn, oid)["status"] == "cancel_requested"
    tick(pool, ex, client, now)
    row, a = order_row(conn, oid), assignment_row(conn, s.assignment["id"])
    assert row["status"] == "cancelled" and fake.by_client(oid)["status"] == "canceled"
    assert (a["cash_cents"], a["reserved_cents"]) == (1_000_000, 0)


def test_a_cancel_after_1550_is_refused_and_the_order_fills_at_the_close(conn, pool, fake) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    oid = one(conn, s, qty=3)["order_id"]
    ex, client, now = StockExecutor(), fake.client(), db_now(conn)
    tick(pool, ex, client, now)
    fake.now = ny(DAY, 15, 55)
    with conn.transaction():
        assignments.halt_assignment(conn, s.assignment["id"], "test", "owner@example.com")
    tick(pool, ex, client, now)
    row = order_row(conn, oid)
    assert (row["status"], row["reason"]) == ("open", REFUSED)
    fake.now = ny(DAY, 16, 0)
    fake.run_close()
    tick(pool, ex, client, now)
    row, a = order_row(conn, oid), assignment_row(conn, s.assignment["id"])
    assert (row["status"], row["filled_qty"], row["reserved_cents"]) == ("filled", 3, 0)
    assert a["cash_cents"] == 1_000_000 - 30_450 and fills(conn) == 1


@pytest.mark.parametrize("remote, ours", [("canceled", "cancelled"), ("expired", "expired"), ("rejected", "rejected_by_exchange")])
def test_an_order_closed_at_alpaca_is_closed_here_with_the_release(conn, pool, fake, remote, ours) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    oid = one(conn, s, qty=4)["order_id"]
    ex, client, now = StockExecutor(), fake.client(), db_now(conn)
    tick(pool, ex, client, now)
    fake.fill(fake.by_client(oid)["id"], 1, "100")
    fake.by_client(oid)["status"] = remote
    tick(pool, ex, client, now)
    row, a = order_row(conn, oid), assignment_row(conn, s.assignment["id"])
    assert (row["status"], row["filled_qty"], row["reserved_cents"], a["reserved_cents"]) == (ours, 1, 0, 0)
    assert a["cash_cents"] == 1_000_000 - 10_000


def test_nothing_is_submitted_under_kill_stale_session_or_other_keys(conn, pool, fake) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    oid = one(conn, s, qty=1)["order_id"]
    conn.execute("UPDATE settings SET value = 'true' WHERE key = 'kill_switch'")
    result = tick(pool, StockExecutor(), fake.client(), db_now(conn))
    assert result["cancelled_under_kill"] == 1 and order_row(conn, oid)["status"] == "cancelled" and posts(fake) == 0
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'kill_switch'")
    oid = one(conn, s, qty=1)["order_id"]
    conn.execute("UPDATE stock_broker_state SET session_date = session_date + 1")
    tick(pool, StockExecutor(), fake.client(), db_now(conn))
    assert (order_row(conn, oid)["status"], order_row(conn, oid)["reason"]) == ("cancelled", "its session is over")
    conn.execute("UPDATE stock_broker_state SET session_date = session_date - 1")
    oid = one(conn, s, qty=1)["order_id"]
    live = FakeAlpaca(now=ny(DAY, 11), environment="live")
    tick(pool, StockExecutor(), live.client(), db_now(conn))
    assert order_row(conn, oid)["status"] == "cancelled" and posts(fake) == 0 and posts(live) == 0
    assert assignment_row(conn, s.assignment["id"])["cash_cents"] == 1_000_000


def test_no_task_touches_alpaca_without_keys(conn, pool, monkeypatch) -> None:
    for name in (ac.KEY_VAR, ac.SECRET_VAR, ac.BASE_URL_VAR):
        monkeypatch.delenv(name, raising=False)

    def no_http(*args: Any) -> Any:
        raise AssertionError("Alpaca was called without keys")

    monkeypatch.setattr("host.exchange.adapters.live_http.urllib_http", no_http)
    monkeypatch.setattr("host.exchange.alpaca_trading.urllib_http", no_http)
    conn.execute("UPDATE stock_broker_state SET keys_present = true")
    results = StockTasks(pool).run_due(force=True)
    assert results == {name: {"skipped": NO_KEYS} for name in ("stock_broker", "stock_executor", "stock_marks")}
    assert conn.execute("SELECT keys_present FROM stock_broker_state").fetchone()["keys_present"] is False
    results = run_once(pool, None, ExchangeLoop(pool))
    assert results["stock_executor"] == {"skipped": NO_KEYS} and results["stock_marks"] == {"skipped": NO_KEYS}
    monkeypatch.setenv(ac.KEY_VAR, "PKX")
    monkeypatch.setenv(ac.SECRET_VAR, "secret")
    monkeypatch.setenv(ac.BASE_URL_VAR, "https://evil.example.com")
    result = StockTasks(pool).run_due(force=True)["stock_broker"]
    assert "not an Alpaca trading host" in result["skipped"]


def test_the_tasks_with_keys_check_the_broker_and_run_the_outbox(conn, pool, fake) -> None:
    s = stock_setup(conn)
    oid = one(conn, s, qty=1)["order_id"]
    fake.now, fake.extra_days = ny(s.session, 11), {s.session}
    tasks = StockTasks(pool, clock=lambda: db_now(conn), client_factory=fake.client)
    results = tasks.run_due(force=True)
    assert results["stock_broker"]["market_open"] is True and results["stock_broker"]["mismatches"] == []
    assert results["stock_executor"]["submitted"] == 1 and order_row(conn, oid)["status"] == "open"
    broker = conn.execute("SELECT * FROM stock_broker_state").fetchone()
    assert broker["keys_present"] and broker["environment"] == "paper" and broker["session_date"] == s.session
    assert tasks.due("stock_executor", db_now(conn) + timedelta(seconds=1.1))
    assert not tasks.due("stock_broker", db_now(conn) + timedelta(seconds=5))
