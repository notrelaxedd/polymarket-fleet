"""The stock_orders row moves shared by the exchange's stock executor and the operator
commands (host/exchange/stock_ops.py): a status change with its event, the close with
the release, and applying what Alpaca says about an order (fills first, then its
closed state). Rows are locked assignment first, then the order: the lock order of the
approval and the kill, so none of them deadlocks with another.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from host.exchange import stock_fills
from host.exchange.stock_broker import parse_ts, qty_of
from host.stocks import orders as stock_orders

ACTOR = "exchange"
ACTIVE = ("submitting", "open", "partial", "cancel_requested")
REMOTE_CLOSED = {"canceled": "cancelled", "expired": "expired", "done_for_day": "expired", "rejected": "rejected_by_exchange"}


def set_status(conn: psycopg.Connection, order_id: Any, to_status: str, expected: tuple[str, ...],
               detail: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any] | None:
    """Move one row from an expected status (None when it was elsewhere) with an event."""
    before = conn.execute("SELECT status FROM stock_orders WHERE id = %s FOR UPDATE", (order_id,)).fetchone()
    if before is None or before["status"] not in expected:
        return None
    sets = ", ".join([f"{k} = %s" for k in fields] + ["status = %s", "updated_at = now()"])
    row = conn.execute(f"UPDATE stock_orders SET {sets} WHERE id = %s RETURNING *",
                       [*fields.values(), to_status, order_id]).fetchone()
    stock_orders.add_event(conn, order_id, before["status"], to_status, ACTOR, detail)
    return dict(row)


def lock_pair(conn: psycopg.Connection, order_id: Any) -> dict[str, Any] | None:
    """The order's assignment row, then the order row, FOR UPDATE; the order."""
    conn.execute("SELECT 1 FROM stock_assignments WHERE id = (SELECT assignment_id FROM stock_orders WHERE id = %s) FOR UPDATE",
                 (order_id,))
    row = conn.execute("SELECT * FROM stock_orders WHERE id = %s FOR UPDATE", (order_id,)).fetchone()
    return dict(row) if row is not None else None


def close(conn: psycopg.Connection, order_id: Any, to_status: str, reason: str, detail: dict[str, Any] | None = None) -> bool:
    """An active row -> a closed status with what it still reserves released."""
    row = lock_pair(conn, order_id)
    if row is None or row["status"] not in ACTIVE + ("approved",):
        return False
    released = stock_orders.release(conn, row)
    set_status(conn, order_id, to_status, (row["status"],), {"reason": reason, "released_cents": released, **(detail or {})},
               reason=reason[:200])
    return True


def apply_remote(conn: psycopg.Connection, order_id: Any, remote: dict[str, Any], now: datetime) -> str:
    """Book what Alpaca filled beyond our filled_qty, then close the row when Alpaca
    closed the order; the row's status afterwards. filled_qty is read under the row lock,
    so two bookers (the exchange and stock-cancel-all --direct) never book the same
    shares twice."""
    row = lock_pair(conn, order_id)
    if row is None:
        return "unknown"
    eid = str(remote.get("id") or row["exchange_order_id"])
    filled = int(qty_of(remote.get("filled_qty")))
    have = int(row["filled_qty"])
    if filled > have:
        avg = float(qty_of(remote.get("filled_avg_price")))
        price = avg
        if have and row["avg_fill_price"]:
            price = (avg * filled - float(row["avg_fill_price"]) * have) / (filled - have)
            price = price if price > 0 else avg
        ts = parse_ts(remote.get("filled_at")) or now
        if not stock_fills.book_fill(conn, row["id"], filled - have, round(price, 6), f"{eid}:{filled}", ts):
            current = conn.execute("SELECT status, filled_qty FROM stock_orders WHERE id = %s", (order_id,)).fetchone()
            if int(current["filled_qty"]) < filled:
                return current["status"]  # refused: never closed over a fill the books do not show
    to_status = REMOTE_CLOSED.get(str(remote.get("status") or ""))
    if to_status is not None:
        close(conn, order_id, to_status, f"{remote.get('status')} at Alpaca", {"exchange_order_id": eid})
    status = conn.execute("SELECT status FROM stock_orders WHERE id = %s", (order_id,)).fetchone()["status"]
    return str(status)
