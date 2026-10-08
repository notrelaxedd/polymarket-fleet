"""Shared setup of the host stock tests (test_stock_host, test_stock_limits,
test_stock_kill): instruments with daily bars, the broker row, stock models, an
assignment whose stock_trade job is leased to a worker, and order rows."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from host.stocks import approve, assignments
from host.stocks.market import previous_weekday
from tests.conftest import FakeWorker, insert_worker

NY = ZoneInfo("America/New_York")
GOOD_BT = {"sharpe": 1.2, "max_drawdown": 0.12, "trades": 80, "cagr": 0.11, "first_day": "2017-01-03",
           "last_day": "2023-12-29"}
GOOD_VAL = {"sharpe": 0.8, "max_drawdown": 0.10, "trades": 30}


def db_now(conn: psycopg.Connection) -> datetime:
    return conn.execute("SELECT now() AS n").fetchone()["n"]


def session_of(next_close: datetime) -> date:
    return next_close.astimezone(NY).date()


def add_bars(conn: psycopg.Connection, symbol: str, days: list[date], close: float = 100.0,
             tradable: bool = True, fetched_at: datetime | None = None) -> None:
    """An instrument with one bar per day (midnight New York, as Alpaca stamps them)."""
    conn.execute(
        "INSERT INTO instruments (symbol, tradable, fetched_at) VALUES (%s, %s, %s)"
        " ON CONFLICT (symbol) DO UPDATE SET tradable = EXCLUDED.tradable, fetched_at = EXCLUDED.fetched_at",
        (symbol, tradable, fetched_at or datetime.now(timezone.utc)),
    )
    for day in days:
        ts = datetime.combine(day, dtime(0, 0), tzinfo=NY)
        conn.execute(
            "INSERT INTO stock_bars (symbol, timeframe, ts, open, high, low, close, volume, feed)"
            " VALUES (%s, '1Day', %s, %s, %s, %s, %s, 1000, 'sip') ON CONFLICT DO NOTHING",
            (symbol, ts, close, close, close, close),
        )
    conn.execute(
        "UPDATE instruments SET bars_count = (SELECT count(*) FROM stock_bars WHERE symbol = %s),"
        " bars_through = (SELECT max(ts) FROM stock_bars WHERE symbol = %s) WHERE symbol = %s",
        (symbol, symbol, symbol),
    )


def set_broker(conn: psycopg.Connection, **fields: Any) -> dict[str, Any]:
    """The broker row as a fresh paper check during an open session that closes in 15
    minutes (inside the decision window, before the 11 minute cutoff); `fields` override."""
    now = db_now(conn)
    close = fields.pop("next_close", now + timedelta(minutes=15))
    values = {"environment": "paper", "keys_present": True, "account_status": "ACTIVE", "equity_cents": 10_000_000,
              "cash_cents": 10_000_000, "buying_power_cents": 20_000_000, "market_open": True,
              "session_date": session_of(close), "next_open": close + timedelta(hours=17), "next_close": close,
              "checked_at": now, "last_error": None}
    values.update(fields)
    conn.execute(
        "UPDATE stock_broker_state SET " + ", ".join(f"{k} = %s" for k in values) + " WHERE id = 1", list(values.values())
    )
    return values


def insert_stock_model(conn: psycopg.Connection, status: str = "paper_ok", family: str = "momentum",
                       params: dict[str, Any] | None = None, backtest: dict[str, Any] | None = GOOD_BT,
                       validation: dict[str, Any] | None = GOOD_VAL) -> dict[str, Any]:
    params = params if params is not None else {"lookback": 126, "skip": 5, "top_k": 3, "seed": uuid.uuid4().hex[:6]}
    row = conn.execute(
        """
        INSERT INTO stock_models (family, params, params_hash, status, backtest_metrics, validation_metrics, summary)
        VALUES (%s, %s, %s, %s, %s, %s, 'test model') RETURNING *
        """,
        (family, Jsonb(params), uuid.uuid4().hex[:16], status, Jsonb(backtest) if backtest else None,
         Jsonb(validation) if validation else None),
    ).fetchone()
    conn.execute("UPDATE stock_models SET lineage_id = id WHERE id = %s", (row["id"],))
    return dict(row)


def lease(conn: psycopg.Connection, job_id: Any, worker: FakeWorker) -> uuid.UUID:
    token = uuid.uuid4()
    conn.execute(
        "UPDATE jobs SET status = 'leased', lease_worker_id = %s, lease_token = %s,"
        " lease_expires_at = now() + interval '10 minutes' WHERE id = %s",
        (worker.id, token, job_id),
    )
    return token


@dataclass
class StockSetup:
    worker: FakeWorker
    model: dict[str, Any]
    assignment: dict[str, Any]
    job_id: str
    lease_token: uuid.UUID
    session: date
    broker: dict[str, Any]

    def body(self, orders: list[dict[str, Any]], session: date | None = None) -> dict[str, Any]:
        return {"job_id": self.job_id, "lease_token": str(self.lease_token), "assignment_id": self.assignment["id"],
                "session_date": (session or self.session).isoformat(), "orders": orders}


def stock_setup(conn: psycopg.Connection, mode: str = "paper", bankroll: int = 1_000_000,
                symbols: tuple[str, ...] = ("SPY", "AAPL"), worker: FakeWorker | None = None,
                model_status: str | None = None) -> StockSetup:
    """Bars through the previous weekday, a fresh broker, a model and an assignment whose
    job is leased to a trade worker."""
    broker = set_broker(conn, environment=mode)
    session = broker["session_date"]
    prev = previous_weekday(session)
    days = [prev - timedelta(days=7 * k) for k in range(3)] + [prev]
    for symbol in symbols:
        add_bars(conn, symbol, sorted(set(days)), close=100.0 if symbol != "SPY" else 400.0)
    if mode == "live":
        conn.execute("UPDATE settings SET value = 'true' WHERE key = 'live_enabled'")
    model = insert_stock_model(conn, status=model_status or ("live_eligible" if mode == "live" else "paper_ok"))
    worker = worker or insert_worker(conn, f"stock-{uuid.uuid4().hex[:6]}", role="trade")
    with conn.transaction():
        a = assignments.create_assignment(conn, model["id"], mode, bankroll, list(symbols), "owner@example.com")
    token = lease(conn, a["job_id"], worker)
    return StockSetup(worker, model, a, a["job_id"], token, session, broker)


def request(conn: psycopg.Connection, s: StockSetup, orders: list[dict[str, Any]], session: date | None = None) -> list[dict[str, Any]]:
    worker = conn.execute("SELECT * FROM workers WHERE id = %s", (s.worker.id,)).fetchone()
    with conn.transaction():
        return approve.request_orders(conn, worker, s.body(orders, session))["orders"]


def order(symbol: str = "AAPL", side: str = "buy", qty: int = 1, ref: int = 10_000, crid: str | None = None) -> dict[str, Any]:
    return {"client_request_id": crid or uuid.uuid4().hex[:24], "symbol": symbol, "side": side, "qty": qty,
            "ref_price_cents": ref, "rationale": "test"}


def one(conn: psycopg.Connection, s: StockSetup, **kw: Any) -> dict[str, Any]:
    return request(conn, s, [order(**kw)])[0]


def assignment_row(conn: psycopg.Connection, aid: Any) -> dict[str, Any]:
    return conn.execute("SELECT * FROM stock_assignments WHERE id = %s", (aid,)).fetchone()


def order_row(conn: psycopg.Connection, oid: Any) -> dict[str, Any]:
    return conn.execute("SELECT * FROM stock_orders WHERE id = %s", (uuid.UUID(str(oid)),)).fetchone()


def set_position(conn: psycopg.Connection, aid: Any, symbol: str, qty: int, cost_cents: int | None = None) -> None:
    conn.execute(
        "INSERT INTO stock_positions (assignment_id, symbol, qty, cost_cents) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (assignment_id, symbol) DO UPDATE SET qty = EXCLUDED.qty, cost_cents = EXCLUDED.cost_cents",
        (aid, symbol, qty, cost_cents if cost_cents is not None else qty * 10_000),
    )


def set_order_status(conn: psycopg.Connection, oid: Any, status: str, filled: int = 0) -> None:
    conn.execute("UPDATE stock_orders SET status = %s, filled_qty = %s, exchange_order_id = %s WHERE id = %s",
                 (status, filled, f"ex-{oid}", uuid.UUID(str(oid))))


def events(conn: psycopg.Connection, oid: Any) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM stock_order_events WHERE order_id = %s ORDER BY id", (uuid.UUID(str(oid)),)).fetchall()


def audit(conn: psycopg.Connection, action: str) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM audit_log WHERE action = %s ORDER BY id", (action,)).fetchall()


def set_setting(conn: psycopg.Connection, key: str, value: Any) -> None:
    conn.execute("UPDATE settings SET value = %s WHERE key = %s", (Jsonb(value), key))
