"""Shared helpers for the step 6 Part B sell tests (not a test module itself)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import psycopg

from host.trading import orders
from tests.test_exchange import make_order

FEE = {"taker_rate": 0.05, "half_spread": 0.01}


def bought(conn: psycopg.Connection, assignment: dict[str, Any], market: dict[str, Any], price: float, size: int,
           fee_cents: int, submitted_at: datetime | None = None, **kw: Any) -> dict[str, Any]:
    """A buy order (reservation posted) filled in one fill of `size` at `price`."""
    o = make_order(conn, assignment, market, price, size, status="open", submitted_at=submitted_at, **kw)
    return orders.record_fill(conn, o["id"], price, size, fee_cents, assignment["mode"], "test")


def sell_order(conn: psycopg.Connection, assignment: dict[str, Any], market: dict[str, Any], price: float, size: int,
               status: str = "open", submitted_at: datetime | None = None, worker_id: str | None = None,
               my_p: float | None = 0.5, market_p: float | None = 0.6, edge: float | None = 0.05) -> dict[str, Any]:
    """A sell order row as approve_sell writes it: side 'sell', cost_cents 0, no ledger row."""
    row = conn.execute(
        """
        INSERT INTO orders (client_request_id, assignment_id, worker_id, job_id, market_id, mode, side, price, size,
                            cost_cents, fee_cents_est, status, submitted_at, my_p, market_p, edge, rationale)
        VALUES (%s, %s, %s, %s, %s, %s, 'sell', %s, %s, 0, 0, %s, %s, %s, %s, %s, 'test sell') RETURNING *
        """,
        (uuid.uuid4().hex[:32], assignment["id"], worker_id, assignment.get("job_id"), market["id"], assignment["mode"],
         price, size, status, submitted_at, my_p, market_p, edge),
    ).fetchone()
    orders.add_order_event(conn, row["id"], None, status, "test")
    return dict(row)


def sold(conn: psycopg.Connection, assignment: dict[str, Any], market: dict[str, Any], price: float, size: int,
         fee_cents: int, **kw: Any) -> dict[str, Any]:
    """A sell order filled in one fill of `size` at `price`."""
    o = sell_order(conn, assignment, market, price, size, **kw)
    return orders.record_fill(conn, o["id"], price, size, fee_cents, assignment["mode"], "test")


def ledger_rows(conn: psycopg.Connection, order_id: Any, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ledger WHERE ref_type = 'order' AND ref_id = %s" + (" AND kind = %s" if kind else "") + " ORDER BY id"
    return conn.execute(sql, (str(order_id), kind) if kind else (str(order_id),)).fetchall()
