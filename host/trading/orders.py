"""Order rows, their state transitions and the order_events audit trail."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from host.errors import Conflict, NotFound
from host.trading import ledger

ACTIVE_STATUSES = ("approved", "submitting", "open", "partial", "cancel_requested")
OPEN_ON_EXCHANGE = ("submitting", "open", "partial")
TERMINAL_STATUSES = ("rejected", "filled", "cancelled", "rejected_by_exchange", "expired")


def cents(amount: Any) -> int:
    """Dollars to whole cents, rounded half up (the same rule as SQL ROUND), exact
    for the decimal prices the exchange quotes (0.985 is not a float boundary)."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fill_cost_cents(price: Any, size: int) -> int:
    """price * size in cents, rounded half up: the one rounding used by fills, the
    ledger, settlement and the paper simulator."""
    return cents(Decimal(str(price)) * int(size))


def get_order(conn: psycopg.Connection, order_id: Any, for_update: bool = False) -> dict[str, Any]:
    sql = "SELECT * FROM orders WHERE id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (order_id,)).fetchone()
    if row is None:
        raise NotFound(f"unknown order {order_id}")
    return dict(row)


def add_order_event(
    conn: psycopg.Connection,
    order_id: Any,
    from_status: str | None,
    to_status: str | None,
    actor: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO order_events (order_id, from_status, to_status, actor, detail) VALUES (%s, %s, %s, %s, %s)",
        (order_id, from_status, to_status, actor, Jsonb(detail) if detail is not None else None),
    )


def set_status(
    conn: psycopg.Connection,
    order_id: Any,
    to_status: str,
    actor: str | None,
    detail: dict[str, Any] | None = None,
    expected: Iterable[str] | None = None,
    **columns: Any,
) -> dict[str, Any]:
    """Move an order to `to_status` (optionally only from `expected` statuses), set extra
    columns, and record the transition. 409 when the order is not in an expected status."""
    order = get_order(conn, order_id, for_update=True)
    if expected is not None and order["status"] not in tuple(expected):
        raise Conflict(f"order {order_id} is {order['status']}, expected {', '.join(expected)}")
    sets = ["status = %s", "updated_at = now()"]
    values: list[Any] = [to_status]
    for name, value in columns.items():
        sets.append(f"{name} = %s")
        values.append(value)
    values.append(order_id)
    row = conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = %s RETURNING *", values).fetchone()
    add_order_event(conn, order_id, order["status"], to_status, actor, detail)
    return dict(row)


def remaining_reservation_cents(conn: psycopg.Connection, order: dict[str, Any]) -> int:
    """What is left of the order's reservation: the approved cost minus everything its
    fills consumed (cost plus fees), never negative, and zero once released."""
    if order.get("assignment_id") is None:
        return 0
    released = conn.execute(
        "SELECT COALESCE(SUM(-d_reserved), 0) AS s FROM ledger WHERE ref_type = 'order' AND ref_id = %s AND kind = 'release'",
        (str(order["id"]),),
    ).fetchone()["s"]
    remaining = int(order["cost_cents"]) - sum_consumed(conn, order["id"]) - int(released)
    return max(0, remaining)


def release_unfilled(conn: psycopg.Connection, order: dict[str, Any], note: str | None = None) -> int:
    """Give whatever is left of the reservation back to the bankroll (no-op for smoke orders)."""
    cents = remaining_reservation_cents(conn, order)
    if cents <= 0:
        return 0
    bank = ledger.bankroll_for_assignment(conn, order["assignment_id"])
    ledger.release(conn, bank["id"], cents, order["id"], note=note)
    return cents


def cancel_order(conn: psycopg.Connection, order_id: Any, actor: str | None, reason: str) -> str:
    """Cancel an order. `approved` (never submitted) and every paper order are cancelled at
    once with the ledger release; a live order on the exchange becomes `cancel_requested`
    for the exchange process to complete. Terminal orders are left alone."""
    order = get_order(conn, order_id, for_update=True)
    status = order["status"]
    if status in TERMINAL_STATUSES or status == "cancel_requested":
        return status
    if status == "approved" or order["mode"] == "paper":
        release_unfilled(conn, order, note=reason)
        set_status(conn, order_id, "cancelled", actor, {"reason": reason}, expected=ACTIVE_STATUSES)
        return "cancelled"
    set_status(conn, order_id, "cancel_requested", actor, {"reason": reason}, expected=OPEN_ON_EXCHANGE)
    return "cancel_requested"


def confirm_cancelled(conn: psycopg.Connection, order_id: Any, actor: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """The exchange confirmed a cancel: release the unfilled part and close the order."""
    order = get_order(conn, order_id, for_update=True)
    release_unfilled(conn, order, note="cancel confirmed")
    return set_status(conn, order_id, "cancelled", actor, detail, expected=("cancel_requested", "submitting", "open", "partial"))


def record_fill(
    conn: psycopg.Connection,
    order_id: Any,
    price: float,
    size: int,
    fee_cents: int,
    mode: str,
    actor: str,
    snapshot_id: int | None = None,
    exchange_fill_id: str | None = None,
) -> dict[str, Any]:
    """Append a fill, move money (`ledger.fill`), update filled_size/avg price and the
    status (`partial` or `filled`). Idempotent on `exchange_fill_id`."""
    if exchange_fill_id is not None:
        seen = conn.execute("SELECT 1 FROM fills WHERE exchange_fill_id = %s", (exchange_fill_id,)).fetchone()
        if seen:
            return get_order(conn, order_id)
    order = get_order(conn, order_id, for_update=True)
    if order["status"] not in ("open", "partial", "submitting", "cancel_requested"):
        raise Conflict(f"order {order_id} is {order['status']}, cannot fill")
    remaining = int(order["size"]) - int(order["filled_size"])
    if size <= 0 or size > remaining:
        raise Conflict(f"fill of {size} exceeds the {remaining} unfilled contracts of order {order_id}")
    conn.execute(
        """
        INSERT INTO fills (order_id, price, size, fee_cents, mode, exchange_fill_id, snapshot_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (order_id, price, size, fee_cents, mode, exchange_fill_id, snapshot_id),
    )
    cost_cents = fill_cost_cents(price, size)
    if order.get("assignment_id") is not None:
        bank = ledger.bankroll_for_assignment(conn, order["assignment_id"])
        ledger.fill(conn, bank["id"], cost_cents, fee_cents, order_id)
    filled = int(order["filled_size"]) + size
    prev_avg = float(order["avg_fill_price"] or 0.0)
    avg = (prev_avg * int(order["filled_size"]) + float(price) * size) / filled
    new_status = "filled" if filled >= int(order["size"]) else ("partial" if order["status"] != "cancel_requested" else "cancel_requested")
    row = set_status(
        conn, order_id, new_status, actor,
        {"fill": {"price": float(price), "size": size, "fee_cents": fee_cents}},
        filled_size=filled, avg_fill_price=round(avg, 6),
    )
    if new_status == "filled":
        # Fees were estimated at approval; whatever reservation is left after the last
        # fill goes back to the bankroll.
        release_unfilled(conn, row, note="reservation surplus after fills")
    return row


def sum_consumed(conn: psycopg.Connection, order_id: Any) -> int:
    """Cents moved out of the reservation by this order's fills (cost + fees), read
    from the ledger's `fill` rows so it can never disagree with what was posted."""
    row = conn.execute(
        "SELECT COALESCE(SUM(-d_reserved), 0) AS s FROM ledger WHERE ref_type = 'order' AND ref_id = %s AND kind = 'fill'",
        (str(order_id),),
    ).fetchone()
    return int(row["s"])
