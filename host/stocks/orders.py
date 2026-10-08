"""Stock order rows: the stock_order_events trail, the reservation release and the
scoped cancel the halt, the kill, live off and the worker release share.

A cancel never talks to Alpaca: an `approved` order (never submitted) becomes
`cancelled` at once with its reservation moved back to the assignment's cash; an order
that may be at Alpaca (`submitting`, `open`, `partial`) becomes `cancel_requested` for
the exchange process (host/exchange/stock_executor.py), which confirms it or keeps it
open with "cancel refused (after 15:50)". `cancel_requested` rows are left alone.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

ACTIVE_STATUSES = ("approved", "submitting", "open", "partial", "cancel_requested")
AT_EXCHANGE = ("submitting", "open", "partial")
ACTIVE_LIST = "', '".join(ACTIVE_STATUSES)
SYSTEM_ACTORS = ("eligibility", "startup")


def event_actor(actor: str | None) -> str:
    """The stock_order_events actor for an audit actor: "host", "exchange" and prefixed
    actors ("worker:<id>", "owner:<login>", "auto:<reason>") as they are, the host's own
    jobs as "host", anyone else (an owner login, the CLI) as "owner:<name>"."""
    if actor is None or actor in SYSTEM_ACTORS:
        return "host"
    if ":" in actor or actor in ("host", "exchange"):
        return actor
    return f"owner:{actor}"


def add_event(
    conn: psycopg.Connection, order_id: Any, from_status: str | None, to_status: str, actor: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """One stock_order_events row (actor "host", "worker:<id>", "exchange" or "owner:<login>")."""
    conn.execute(
        "INSERT INTO stock_order_events (order_id, from_status, to_status, actor, detail) VALUES (%s, %s, %s, %s, %s)",
        (order_id, from_status, to_status, actor, Jsonb(detail) if detail is not None else None),
    )


def release(conn: psycopg.Connection, order: dict[str, Any]) -> int:
    """Move what the order still reserves back to its assignment's cash; the cents moved."""
    amount = int(order.get("reserved_cents") or 0)
    if amount <= 0:
        return 0
    conn.execute("UPDATE stock_orders SET reserved_cents = 0, updated_at = now() WHERE id = %s", (order["id"],))
    conn.execute(
        "UPDATE stock_assignments SET cash_cents = cash_cents + %s, reserved_cents = reserved_cents - %s,"
        " updated_at = now() WHERE id = %s",
        (amount, amount, order["assignment_id"]),
    )
    return amount


def cancel_orders(
    conn: psycopg.Connection,
    actor: str | None,
    reason: str,
    *,
    assignment_ids: list[int] | None = None,
    mode: str | None = None,
    approved_only: bool = False,
) -> dict[str, list[Any]]:
    """Cancel the active orders matching the filters (all of them without filters).

    Returns {"cancelled": [ids now cancelled], "requested": [ids now cancel_requested]}.
    The assignment rows are locked before the orders (the approval's lock order)."""
    clauses = ["status IN ('approved')" if approved_only else "status IN ('approved', 'submitting', 'open', 'partial')"]
    params: list[Any] = []
    if assignment_ids is not None:
        clauses.append("assignment_id = ANY(%s::bigint[])")
        params.append([int(a) for a in assignment_ids])
    if mode is not None:
        clauses.append("mode = %s")
        params.append(mode)
    where = " AND ".join(clauses)
    conn.execute(
        f"SELECT id FROM stock_assignments WHERE id IN (SELECT assignment_id FROM stock_orders WHERE {where})"
        " ORDER BY id FOR UPDATE",
        params,
    )
    rows = conn.execute(f"SELECT * FROM stock_orders WHERE {where} ORDER BY created_at, id FOR UPDATE", params).fetchall()
    cancelled: list[Any] = []
    requested: list[Any] = []
    for row in rows:
        order = dict(row)
        to_status = "cancelled" if order["status"] == "approved" else "cancel_requested"
        conn.execute(
            "UPDATE stock_orders SET status = %s, reason = %s, updated_at = now() WHERE id = %s",
            (to_status, reason, order["id"]),
        )
        if to_status == "cancelled":
            release(conn, order)
            cancelled.append(order["id"])
        else:
            requested.append(order["id"])
        add_event(conn, order["id"], order["status"], to_status, event_actor(actor), {"reason": reason})
    return {"cancelled": cancelled, "requested": requested}


def open_orders(conn: psycopg.Connection, assignment_id: int) -> list[dict[str, Any]]:
    """The assignment's active orders, oldest first."""
    rows = conn.execute(
        f"""
        SELECT id, client_request_id, session_date, symbol, side, qty, filled_qty, ref_price_cents, reserved_cents,
               status, reason, exchange_order_id, created_at
          FROM stock_orders WHERE assignment_id = %s AND status IN ('{ACTIVE_LIST}') ORDER BY created_at, id
        """,
        (assignment_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def positions(conn: psycopg.Connection, assignment_id: int) -> dict[str, int]:
    """{symbol: qty} of the assignment's non-zero positions."""
    rows = conn.execute(
        "SELECT symbol, qty FROM stock_positions WHERE assignment_id = %s AND qty > 0 ORDER BY symbol", (assignment_id,)
    ).fetchall()
    return {r["symbol"]: int(r["qty"]) for r in rows}
