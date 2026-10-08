"""Owner "Sell all" of a halted stock assignment: the way out for the shares of an
assignment that can never trade again (its model retired, or demoted out of
live_eligible), so it can be closed.

A halted assignment never decides and the approval rejects its orders with `halted`
(and a live one with `not_live_eligible` once its model was demoted), while Close needs
no positions: without this, its shares would stay at Alpaca for good. Liquidation
places one market-on-close sell per held symbol, for the shares held minus those
already in open sells, and runs every approval check except `halted`,
`not_live_eligible` and `max_order` (a whole position must be sellable in one order):
kill, environment, broker_stale, live_disabled, market_closed, moc_cutoff, session,
symbol and short still apply. When one sell fails a check nothing is stored and the
refusal names the check. The sells are `approved` rows the stock executor places like
any other; their fills are booked by stock_fills, and once the positions reach zero
Close succeeds.

client_request_id is "liquidate:<session_date>:<symbol>" (a suffix ":<n>" when an
earlier liquidation of that session ended without filling), so a double submit stores
nothing new: the shares are already in open sells.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import psycopg

from host.errors import Conflict
from host.events import add_audit
from host.kill import approval_lock
from host.stocks import approve, market, orders
from host.stocks.assignments import _killed_locked, get_assignment

SKIPPED = ("halted", "not_live_eligible", "max_order")
RATIONALE = "owner liquidation"


def approve_liquidation(
    conn: psycopg.Connection, assignment: dict[str, Any], order: dict[str, Any], now: datetime,
) -> tuple[str, str | None]:
    """approve.approve without the checks a liquidation of a halted assignment skips."""
    ctx = approve._ctx(conn, assignment, order, now)
    reason = next((name for name, check in approve.CHECKS if name not in SKIPPED and check(conn, ctx)), None)
    return ("rejected", reason) if reason else ("approved", None)


def _request_id(conn: psycopg.Connection, assignment_id: int, session: Any, symbol: str) -> str:
    base = f"liquidate:{session.isoformat()}:{symbol}"
    n = conn.execute(
        "SELECT count(*) AS n FROM stock_orders WHERE assignment_id = %s AND (client_request_id = %s"
        " OR client_request_id LIKE %s)",
        (assignment_id, base, base + ":%"),
    ).fetchone()["n"]
    return base if not n else f"{base}:{int(n) + 1}"


def _store(conn: psycopg.Connection, a: dict[str, Any], o: dict[str, Any], actor: str) -> dict[str, Any]:
    oid = uuid.uuid4()
    crid = _request_id(conn, int(a["id"]), o["session_date"], o["symbol"])
    conn.execute(
        """
        INSERT INTO stock_orders (id, assignment_id, mode, session_date, client_request_id, symbol, side, qty,
                                  ref_price_cents, reserved_cents, status, reason, rationale)
        VALUES (%s, %s, %s, %s, %s, %s, 'sell', %s, %s, 0, 'approved', NULL, %s)
        """,
        (oid, a["id"], a["mode"], o["session_date"], crid, o["symbol"], o["qty"], o["ref_price_cents"], RATIONALE),
    )
    orders.add_event(conn, oid, None, "approved", orders.event_actor(actor), {"liquidation": True, "reserved_cents": 0})
    return {"order_id": str(oid), "client_request_id": crid, "symbol": o["symbol"], "qty": o["qty"],
            "ref_price_cents": o["ref_price_cents"], "status": "approved"}


def liquidate_assignment(conn: psycopg.Connection, assignment_id: Any, actor: str) -> dict[str, Any]:
    """Approved market-on-close sells of everything a halted assignment holds:
    {"assignment_id", "session_date", "orders": [...]} (empty when every share is
    already in an open sell). 409 when it is not halted, holds nothing, the kill is on,
    or a sell fails one of the checks kept (the code named)."""
    killed = _killed_locked(conn)
    approval_lock(conn, get_assignment(conn, assignment_id)["mode"])
    a = get_assignment(conn, assignment_id, for_update=True)
    if a["status"] != "halted":
        hint = "; halt it first" if a["status"] == "active" else ""
        raise Conflict(f"stock assignment {a['id']} is {a['status']}: only a halted assignment can be sold out{hint}")
    if killed:
        raise Conflict("the kill switch is on; reset it first")
    held = orders.positions(conn, int(a["id"]))
    if not held:
        raise Conflict(f"stock assignment {a['id']} holds no shares; close it instead")
    broker = market.broker_state(conn)
    session = broker.get("session_date")
    if session is None:
        raise Conflict("no broker check yet: the trading session is unknown")
    now = market.server_now(conn)
    prices = market.ref_prices(conn, sorted(held), session)
    planned = []
    for symbol, qty in sorted(held.items()):
        left = qty - approve._open_qty(conn, int(a["id"]), symbol, "sell")
        if left <= 0:
            continue
        ref = prices.get(symbol, {}).get("cents")
        if ref is None:
            raise Conflict(f"no daily close of {symbol} before {session} to price the sell")
        o = {"session_date": session, "symbol": symbol, "side": "sell", "qty": left, "ref_price_cents": ref,
             "ref_ok": True}
        status, reason = approve_liquidation(conn, a, o, now)
        if status != "approved":
            raise Conflict(f"the sell of {left} {symbol} fails the {reason} check")
        planned.append(o)
    stored = [_store(conn, a, o, actor) for o in planned]
    add_audit(conn, "stock_assignment_liquidate", f"stock_assignment:{a['id']}", actor, {"positions": held},
              {"session_date": session.isoformat(), "orders": [{k: s[k] for k in ("order_id", "symbol", "qty")}
                                                                for s in stored]})
    return {"assignment_id": int(a["id"]), "session_date": session, "orders": stored}
