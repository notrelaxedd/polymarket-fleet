"""Stock splits applied to our books (host/exchange/stock_splits.py): the shares held
before the ex date multiplied, shares bought after it kept, the cost basis kept, cash
in lieu of a fraction, the feed asked to fetch the adjusted history again, once only,
not before the ex date's open (and not compared by the reconciliation until then), and
through the stock_broker task so the reconciliation agrees with Alpaca afterwards."""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from host.exchange import alpaca_credentials as ac
from host.exchange import stock_broker, stock_splits
from host.exchange.alpaca_data import AlpacaData
from host.exchange.stock_tasks import StockTasks
from tests.fake_alpaca import KEY, SECRET, FakeAlpaca, ny
from tests.stock_host_helpers import assignment_row, audit, set_position, stock_setup

EX = date(2027, 3, 4)  # a Thursday


def data_client(fake: FakeAlpaca) -> AlpacaData:
    creds = ac.load({ac.KEY_VAR: KEY, ac.SECRET_VAR: SECRET, ac.BASE_URL_VAR: ac.PAPER_URL})
    return AlpacaData(creds, http=fake, min_gap_s=0)


def position(conn: psycopg.Connection, aid: Any, symbol: str = "AAPL") -> dict[str, Any]:
    return conn.execute("SELECT qty, cost_cents FROM stock_positions WHERE assignment_id = %s AND symbol = %s",
                        (aid, symbol)).fetchone()


def bought_after_ex(conn: psycopg.Connection, a: dict[str, Any], qty: int) -> None:
    """A buy of `qty` new shares booked at the ex date's close."""
    oid = uuid.uuid4()
    conn.execute("INSERT INTO stock_orders (id, assignment_id, mode, session_date, client_request_id, symbol, side, qty,"
                 " ref_price_cents, status, filled_qty) VALUES (%s, %s, %s, %s, %s, 'AAPL', 'buy', %s, 2500, 'filled', %s)",
                 (oid, a["id"], a["mode"], EX, uuid.uuid4().hex[:24], qty, qty))
    conn.execute("INSERT INTO stock_fills (order_id, exchange_fill_id, qty, price, cost_cents, ts) VALUES (%s, %s, %s, 25, %s, %s)",
                 (oid, f"x:{qty}", qty, qty * 2500, ny(EX, 16)))


def test_a_forward_split_multiplies_the_shares_held_before_the_ex_date_once(conn) -> None:
    s1 = stock_setup(conn)
    s2 = stock_setup(conn)
    set_position(conn, s1.assignment["id"], "AAPL", 10, 400_000)
    set_position(conn, s2.assignment["id"], "AAPL", 5, 167_500)  # 2 old shares, 3 new ones bought on the ex date
    bought_after_ex(conn, s2.assignment, 3)
    fake = FakeAlpaca(now=ny(EX, 8))
    fake.splits = [{"symbol": "AAPL", "old_rate": 1, "new_rate": 4, "ex_date": EX.isoformat()},
                   {"symbol": "MSFT", "old_rate": 1, "new_rate": 2, "ex_date": EX.isoformat()}]
    watcher, data = stock_splits.SplitWatcher(), data_client(fake)
    out = watcher.run(conn, data, ny(EX, 8))
    assert (out["applied"], out["pending"]) == ([], {"AAPL"}), "not before the ex date's open; MSFT is not held"
    assert position(conn, s1.assignment["id"])["qty"] == 10
    out = watcher.run(conn, data, ny(EX, 9, 31))
    assert [d["symbol"] for d in out["applied"]] == ["AAPL"] and out["pending"] == set()
    assert dict(position(conn, s1.assignment["id"])) == {"qty": 40, "cost_cents": 400_000}
    assert dict(position(conn, s2.assignment["id"])) == {"qty": 11, "cost_cents": 167_500}
    assert conn.execute("SELECT fetched_at FROM instruments WHERE symbol = 'AAPL'").fetchone()["fetched_at"] is None
    (row,) = audit(conn, "stock_split_applied")
    assert row["entity"] == f"stock_split:AAPL:{EX}" and len(row["after"]["assignments"]) == 2
    assert stock_splits.SplitWatcher().run(conn, data, ny(EX, 10))["applied"] == [], "applied once"
    assert position(conn, s1.assignment["id"])["qty"] == 40 and len(audit(conn, "stock_split_applied")) == 1
    assert sum(1 for m, p in fake.calls if p == "/v1/corporate-actions") == 2, "one read a day per watcher"


def test_a_reverse_split_pays_the_fraction_in_cash_at_its_cost(conn) -> None:
    s = stock_setup(conn, bankroll=1_000_000)
    set_position(conn, s.assignment["id"], "AAPL", 15, 150_000)
    split = stock_splits.Split("AAPL", EX - timedelta(days=2), Decimal(10), Decimal(1))
    detail = stock_splits.apply_split(conn, split, ny(EX, 10))
    assert detail["assignments"][0]["cash_in_lieu_cents"] == 50_000
    assert dict(position(conn, s.assignment["id"])) == {"qty": 1, "cost_cents": 100_000}
    assert assignment_row(conn, s.assignment["id"])["cash_cents"] == 1_000_000 + 50_000
    assert stock_splits.apply_split(conn, split, ny(EX, 11)) is None


def test_the_broker_task_applies_the_split_before_reconciling(conn, pool) -> None:
    s = stock_setup(conn, mode="live")
    set_position(conn, s.assignment["id"], "AAPL", 10, 400_000)
    fake = FakeAlpaca(now=ny(EX, 8), environment="live")
    fake.positions = {"AAPL": Decimal(40)}  # Alpaca has split the account before the open
    fake.splits = [{"symbol": "AAPL", "old_rate": "1", "new_rate": "4", "ex_date": EX.isoformat()}]
    tasks = StockTasks(pool, clock=lambda: fake.now, client_factory=fake.client, data_factory=lambda: data_client(fake))
    for _ in range(2):
        result = tasks.run_task("stock_broker", fake.now)
        assert result["mismatches"] == [] and result["auto_killed"] is None, "pending: not compared"
    fake.now = ny(EX, 9, 31)
    result = tasks.run_task("stock_broker", fake.now)
    assert result["splits_applied"] == ["AAPL"] and result["mismatches"] == [] and result["auto_killed"] is None
    assert position(conn, s.assignment["id"])["qty"] == 40
    assert conn.execute("SELECT value FROM settings WHERE key = 'kill_switch'").fetchone()["value"] is False
    unsplit = stock_broker.Reconciler()
    set_position(conn, s.assignment["id"], "AAPL", 10, 400_000)  # a split nobody applied: live still kills
    unsplit.run(conn, fake.client(), fake.now)
    assert unsplit.run(conn, fake.client(), fake.now)["auto_killed"] == "stock_position_mismatch"
