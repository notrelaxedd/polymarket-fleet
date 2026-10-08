"""Booking a stock fill (contract section 6): `book_fill(conn, order_id, qty, price,
exchange_fill_id, ts) -> bool`, one savepoint.

- buy: cost_cents = round half up(qty * price * 100). The order's reservation drops by
  its share for this fill (all that is left on the fill that completes the order), the
  assignment's reserved drops by the same, its cash takes back that share minus the
  cost (the unused part of the band). The position gains qty and cost. A cls buy fills
  at today's close, which the band over the previous close does not bound: a cost
  above the reservation share is still booked as it is (cash may go below zero, the
  books stay true to the account), with a warning on the broker row, an audit row
  `stock_fill_over_reservation` and the excess in the order event.
- sell: the proceeds go to cash; realized += proceeds - the average cost of the qty
  sold; the position loses qty and that cost.
- Idempotent on exchange_fill_id ("<exchange_order_id>:<cumulative filled_qty>"): a
  fill already booked returns False and changes nothing.
- Refused (nothing booked, False): a fill on a closed order, a fill larger than what
  is left of the order, or a sell larger than the position. That is real money the
  books cannot show: live auto-kills `late_fill`, paper writes a warning on the
  broker row, both an audit row `stock_fill_refused` and an order event.

Locks: the assignment row before the order row, the order every approval and the
kill take, so a fill never deadlocks with them.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg

from host.events import add_audit
from host.exchange import stock_broker
from host.exchange.live_sync import maybe_auto_kill
from host.stocks import orders as stock_orders

log = logging.getLogger(__name__)

ACTOR = "exchange"
FILLABLE = ("submitting", "open", "partial", "cancel_requested")


def money_cents(qty: int, price: Any) -> int:
    """qty * price dollars in whole cents, rounded half up."""
    return int((Decimal(str(price)) * int(qty) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def share(total: int, part: int, whole: int) -> int:
    """total * part / whole rounded half up (all of it when part == whole)."""
    if part >= whole or whole <= 0:
        return int(total)
    return int((Decimal(int(total)) * int(part) / int(whole)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _lock(conn: psycopg.Connection, order_id: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(assignment, order), both locked, assignment first; None when the order is unknown."""
    row = conn.execute("SELECT assignment_id FROM stock_orders WHERE id = %s", (order_id,)).fetchone()
    if row is None:
        return None
    a = conn.execute("SELECT * FROM stock_assignments WHERE id = %s FOR UPDATE", (row["assignment_id"],)).fetchone()
    o = conn.execute("SELECT * FROM stock_orders WHERE id = %s FOR UPDATE", (order_id,)).fetchone()
    return dict(a), dict(o)


def _position(conn: psycopg.Connection, assignment_id: int, symbol: str) -> dict[str, Any]:
    row = conn.execute("SELECT qty, cost_cents FROM stock_positions WHERE assignment_id = %s AND symbol = %s FOR UPDATE",
                       (assignment_id, symbol)).fetchone()
    return {"qty": int(row["qty"]), "cost_cents": int(row["cost_cents"])} if row else {"qty": 0, "cost_cents": 0}


def refusal(o: dict[str, Any], qty: int, held: int) -> str | None:
    """Why this fill cannot be booked on this order (None when it can)."""
    if o["status"] not in FILLABLE:
        return f"fill on a closed order ({o['status']})"
    if qty <= 0 or qty > int(o["qty"]) - int(o["filled_qty"]):
        return f"fill of {qty} larger than the {int(o['qty']) - int(o['filled_qty'])} left on the order"
    if o["side"] == "sell" and qty > held:
        return f"sell of {qty} larger than the position of {held}"
    return None


def refuse(conn: psycopg.Connection, o: dict[str, Any], why: str, detail: dict[str, Any]) -> None:
    """Record a fill the books cannot take: live auto-kills late_fill, paper warns."""
    detail = {"order_id": str(o["id"]), "assignment_id": o["assignment_id"], "symbol": o["symbol"], "side": o["side"],
              "status": o["status"], "why": why, **detail}
    log.error("stock fill refused (%s): %s", o["mode"], detail)
    stock_orders.add_event(conn, o["id"], o["status"], o["status"], ACTOR, {"fill_refused": detail})
    add_audit(conn, "stock_fill_refused", f"stock_order:{o['id']}", ACTOR, None, detail)
    if o["mode"] == "live":
        maybe_auto_kill(conn, "late_fill", detail)
    else:
        stock_broker.add_warning(conn, "fill_refused", f"{o['symbol']} {o['side']} fill not booked: {why}", detail)


def over_reservation(conn: psycopg.Connection, a: dict[str, Any], o: dict[str, Any], qty: int, price: Any,
                     fill_id: str, part: int, cost: int) -> dict[str, Any]:
    """A buy fill that cost more than its share of the reservation (the close moved
    beyond the band): booked as it is, made visible (see the module doc)."""
    over = {"order_id": str(o["id"]), "assignment_id": a["id"], "symbol": o["symbol"], "fill_id": fill_id, "qty": qty,
            "price": float(price), "reserved_part_cents": part, "cost_cents": cost, "over_cents": cost - part}
    log.warning("stock buy fill above its reservation (%s): %s", o["mode"], over)
    add_audit(conn, "stock_fill_over_reservation", f"stock_order:{o['id']}", ACTOR, None, over)
    stock_broker.add_warning(conn, "fill_over_reservation",
                             f"{o['symbol']} buy filled {cost - part} cents above its reservation", over)
    return over


def book_fill(conn: psycopg.Connection, order_id: Any, qty: int, price: float, exchange_fill_id: str,
              ts: datetime) -> bool:
    """Book one fill (see the module doc); True when it was booked."""
    qty = int(qty)
    locked = _lock(conn, order_id)
    if locked is None:
        log.error("fill %s for an unknown stock order %s", exchange_fill_id, order_id)
        return False
    a, o = locked
    if conn.execute("SELECT 1 FROM stock_fills WHERE exchange_fill_id = %s", (exchange_fill_id,)).fetchone():
        return False
    pos = _position(conn, int(a["id"]), o["symbol"])
    why = refusal(o, qty, pos["qty"]) if price and float(price) > 0 else "fill without a positive price"
    if why:
        refuse(conn, o, why, {"fill_id": exchange_fill_id, "qty": qty, "price": price, "held": pos["qty"]})
        return False
    cost = money_cents(qty, price)
    with conn.transaction():
        conn.execute("INSERT INTO stock_fills (order_id, exchange_fill_id, qty, price, cost_cents, ts) VALUES (%s, %s, %s, %s, %s, %s)",
                     (o["id"], exchange_fill_id, qty, float(price), cost, ts))
        over: dict[str, Any] = {}
        if o["side"] == "buy":
            part = share(int(o["reserved_cents"]), qty, int(o["qty"]) - int(o["filled_qty"]))
            conn.execute("UPDATE stock_orders SET reserved_cents = reserved_cents - %s WHERE id = %s", (part, o["id"]))
            conn.execute("UPDATE stock_assignments SET reserved_cents = reserved_cents - %s, cash_cents = cash_cents + %s,"
                         " updated_at = now() WHERE id = %s", (part, part - cost, a["id"]))
            if cost > part:
                over = over_reservation(conn, a, o, qty, price, exchange_fill_id, part, cost)
            conn.execute(
                "INSERT INTO stock_positions (assignment_id, symbol, qty, cost_cents) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (assignment_id, symbol) DO UPDATE SET qty = stock_positions.qty + EXCLUDED.qty,"
                " cost_cents = stock_positions.cost_cents + EXCLUDED.cost_cents, updated_at = now()",
                (a["id"], o["symbol"], qty, cost))
        else:
            sold_cost = share(pos["cost_cents"], qty, pos["qty"])
            conn.execute("UPDATE stock_assignments SET cash_cents = cash_cents + %s, realized_cents = realized_cents + %s,"
                         " updated_at = now() WHERE id = %s", (cost, cost - sold_cost, a["id"]))
            conn.execute("UPDATE stock_positions SET qty = qty - %s, cost_cents = cost_cents - %s, updated_at = now()"
                         " WHERE assignment_id = %s AND symbol = %s", (qty, sold_cost, a["id"], o["symbol"]))
        filled = int(o["filled_qty"]) + qty
        avg = (float(o["avg_fill_price"] or 0) * int(o["filled_qty"]) + float(price) * qty) / filled
        status = "filled" if filled == int(o["qty"]) else ("cancel_requested" if o["status"] == "cancel_requested" else "partial")
        conn.execute("UPDATE stock_orders SET filled_qty = %s, avg_fill_price = %s, status = %s, updated_at = now() WHERE id = %s",
                     (filled, avg, status, o["id"]))
        stock_orders.add_event(conn, o["id"], o["status"], status, ACTOR,
                               {"fill_id": exchange_fill_id, "qty": qty, "price": float(price), "cost_cents": cost,
                                **({"over_reservation_cents": over["over_cents"]} if over else {})})
    return True
