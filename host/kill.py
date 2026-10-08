"""The fleet-wide kill switch: set (with the cancel-all), reset (with confirmation),
read, and the owner's cancel-all without a kill.

The flag lives in settings.kill_switch and is mirrored to workers in every register
and heartbeat reply. It stops trade claims and order approvals; batch roles keep
working. A kill is one transaction under the kill row lock and both approval locks
(docs/TRADING.md "Kill switch"): nothing can be approved while it runs, every active
order is cancelled (paper and never-submitted orders at once with the ledger release,
live orders on the exchange become `cancel_requested`), live trading is disabled and
every active assignment is halted. It works with every worker offline and, for paper,
with the exchange down.

Step 9 (stocks on Alpaca): the kill and live off cover the stock tables too
(`stock_effects`): an approved stock order (never submitted) is cancelled with its
reservation released, one that may be at Alpaca becomes `cancel_requested` for the
exchange process; the kill halts every active stock assignment, live off the live ones.
Positions stay. The stock counts are added to the audit rows only when something changed.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.errors import BadRequest
from host.events import add_audit
from host.settings import get_setting
from host.trading import orders

RESET_CONFIRMATION = "RESUME"
MODES = ("paper", "live")
KILL_ACTOR_NOTE = "kill"


def is_killed(conn: psycopg.Connection) -> bool:
    """True when settings.kill_switch is the JSON boolean true."""
    return get_setting(conn, "kill_switch", False) is True


def approval_lock(conn: psycopg.Connection, mode: str) -> None:
    """The per-mode transaction lock that serialises approvals with the kill."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"approve:{mode}",))


def approval_locks(conn: psycopg.Connection, mode: str | None = None) -> None:
    """Both approval locks (always in the same order), or one mode's."""
    for name in MODES if mode is None else (mode,):
        approval_lock(conn, name)


def _lock(conn: psycopg.Connection) -> bool:
    """Lock the kill_switch row for this transaction and return its current value.

    A press that races a reset (or the other way round) waits for the other
    transaction to commit and then sees what it wrote, so the last press wins and
    the audit order matches the final flag.
    """
    row = conn.execute("SELECT value FROM settings WHERE key = 'kill_switch' FOR UPDATE").fetchone()
    return row is not None and row["value"] is True


def _write(conn: psycopg.Connection, key: str, value: bool) -> None:
    conn.execute("UPDATE settings SET value = %s, updated_at = now() WHERE key = %s", (Jsonb(value), key))


def disable_live(conn: psycopg.Connection) -> bool:
    """live_enabled -> false (and the exchange_state stamp cleared); True when it was on."""
    was_on = get_setting(conn, "live_enabled", False) is True
    if was_on:
        _write(conn, "live_enabled", False)
        conn.execute("UPDATE exchange_state SET live_enabled_at = NULL, live_enabled_by = NULL, updated_at = now() WHERE id = true")
    return was_on


def live_off(conn: psycopg.Connection, actor: str, reason: str) -> dict[str, Any]:
    """Turn live trading off at once (docs/LIVE.md "Live switch"): `live_enabled` false,
    every active live assignment halted (its orders cancelled through
    `halt_assignment`, so live rows become `cancel_requested` for the exchange), any
    remaining live order (a smoke order, a row of a halted assignment) cancelled the
    same way, one `live_off` audit row whose `orders_cancel_requested` counts every
    live order now awaiting the exchange's confirmation. Runs under the live approval
    lock, so no live approval can commit in between. Used by `POST /live/off`, the
    daily-loss trip and the owner's Disable button."""
    from host.trading import assignments

    approval_lock(conn, "live")
    was_on = disable_live(conn)
    halted = assignments.halt_live_assignments(conn, actor, reason)
    rest = cancel_active_orders(conn, actor, reason, mode="live")
    result = {
        "live_enabled": False, "was_on": was_on, "reason": reason, "assignments_halted": halted,
        "orders_cancelled": rest["cancelled"], "orders_cancel_requested": len(rest["requested"]),
    }
    stock = stock_effects(conn, actor, reason, "live")
    if stock:
        result["stock"] = stock
    add_audit(conn, "live_off", "live_enabled", actor, {"live_enabled": was_on}, result)
    return result


def stock_effects(conn: psycopg.Connection, actor: str | None, reason: str, mode: str | None) -> dict[str, Any]:
    """Cancel the active stock orders of `mode` (every mode for None, the kill) and halt
    its active stock assignments; the counts, {} when nothing changed.
    The caller holds the approval lock(s)."""
    from host.stocks import assignments as stock_assignments, orders as stock_orders

    rest = stock_orders.cancel_orders(conn, actor, reason, mode=mode)  # first, so the counts name every order
    halted = stock_assignments.halt_mode(conn, mode, reason, actor or "host")
    if not halted and not rest["cancelled"] and not rest["requested"]:
        return {}
    return {"stock_assignments_halted": halted, "stock_orders_cancelled": [str(i) for i in rest["cancelled"]],
            "stock_orders_cancel_requested": [str(i) for i in rest["requested"]]}


def auto_kill(conn: psycopg.Connection, reason: str, detail: dict[str, Any]) -> bool:
    """The exchange process pulls the kill switch itself (docs/LIVE.md "Authentication
    probe and auto-kill"): `set_kill` with actor `auto:<reason>` plus an `auto_kill`
    audit row carrying the reason and the detail. The top bar shows the reason until
    the owner resets with RESUME. Returns True when the flag was off before."""
    actor = f"auto:{reason}"
    flipped = set_kill(conn, actor)
    add_audit(conn, "auto_kill", reason, actor, {"kill_switch": not flipped}, {"reason": reason, **detail})
    return flipped


def _kill_orders(conn: psycopg.Connection, actor: str | None) -> tuple[list[Any], list[Any]]:
    """The scoped CASE update of docs/TRADING.md: returns (cancelled ids, cancel_requested ids).

    Rows that became `cancelled` get their unfilled reservation released; every row
    gets an order_events row. Filled, rejected, expired and already cancelled orders
    are untouched.
    """
    rows = conn.execute(
        """
        WITH k AS (
          SELECT id, status AS from_status FROM orders
           WHERE status IN ('approved', 'submitting', 'open', 'partial')
           ORDER BY created_at FOR UPDATE)
        UPDATE orders o SET
               status = CASE WHEN k.from_status = 'approved' THEN 'cancelled'
                             WHEN o.mode = 'paper' THEN 'cancelled'
                             ELSE 'cancel_requested' END,
               updated_at = now()
          FROM k WHERE o.id = k.id
          RETURNING o.*, k.from_status
        """
    ).fetchall()
    cancelled, requested = [], []
    for row in rows:
        if row["status"] == "cancelled":
            orders.release_unfilled(conn, dict(row), note=KILL_ACTOR_NOTE)
            cancelled.append(row["id"])
        else:
            requested.append(row["id"])
        orders.add_order_event(conn, row["id"], row["from_status"], row["status"], actor, {"reason": "kill"})
    return cancelled, requested


def _halt_assignments(conn: psycopg.Connection) -> list[Any]:
    rows = conn.execute(
        "UPDATE assignments SET status = 'halted', updated_at = now() WHERE status = 'active' RETURNING id"
    ).fetchall()
    return [row["id"] for row in rows]


def set_kill(conn: psycopg.Connection, actor: str | None) -> bool:
    """Raise the kill flag and cancel everything, in one transaction.

    Idempotent; every press writes an audit row `kill` (before/after carry the flag)
    and, when the press actually cancelled, halted or disabled anything, one
    `kill_cancel_all` row with the counts. Returns True when the flag was off before.
    """
    before = _lock(conn)
    approval_locks(conn)
    if not before:
        _write(conn, "kill_switch", True)
    add_audit(conn, "kill", "kill_switch", actor, {"kill_switch": before}, {"kill_switch": True})
    live_was_on = disable_live(conn)
    cancelled, requested = _kill_orders(conn, actor)
    halted = _halt_assignments(conn)
    stock = stock_effects(conn, actor, "kill", None)
    if live_was_on or cancelled or requested or halted or stock:
        add_audit(
            conn, "kill_cancel_all", "orders", actor,
            {"live_enabled": live_was_on},
            {
                "live_enabled": False,
                "orders_cancelled": [str(i) for i in cancelled],
                "orders_cancel_requested": [str(i) for i in requested],
                "assignments_halted": [str(i) for i in halted],
                **stock,
            },
        )
    return not before


def reset_kill(conn: psycopg.Connection, actor: str | None, confirm: str | None) -> None:
    """Clear the kill flag only; `confirm` must be exactly RESUME, otherwise 400 and no
    change. Assignments stay halted (the owner activates them again by hand)."""
    if confirm != RESET_CONFIRMATION:
        raise BadRequest(f'confirmation must be exactly "{RESET_CONFIRMATION}"')
    before = _lock(conn)
    if before:
        _write(conn, "kill_switch", False)
    add_audit(
        conn, "kill_reset", "kill_switch", actor, {"kill_switch": before}, {"kill_switch": False},
        confirmation_text=confirm,
    )


def cancel_active_orders(
    conn: psycopg.Connection,
    actor: str | None,
    reason: str,
    *,
    mode: str | None = None,
    assignment_ids: list[Any] | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Cancel every active order matching the filters through orders.cancel_order.

    Returns {"cancelled": n, "requested": [ids now cancel_requested], "ids": [all touched]}.
    """
    clauses, params = ["status IN ('approved', 'submitting', 'open', 'partial', 'cancel_requested')"], []
    if mode is not None:
        clauses.append("mode = %s")
        params.append(mode)
    if assignment_ids is not None:
        clauses.append("assignment_id = ANY(%s::uuid[])")
        params.append([str(a) for a in assignment_ids])
    if worker_id is not None:
        clauses.append("worker_id = %s")
        params.append(worker_id)
    rows = conn.execute(
        f"SELECT id FROM orders WHERE {' AND '.join(clauses)} ORDER BY created_at", params
    ).fetchall()
    cancelled, requested, touched = 0, [], []
    for row in rows:
        status = orders.cancel_order(conn, row["id"], actor, reason)
        touched.append(row["id"])
        if status == "cancelled":
            cancelled += 1
        elif status == "cancel_requested":
            requested.append(row["id"])
    return {"cancelled": cancelled, "requested": requested, "ids": touched}


def cancel_all(
    conn: psycopg.Connection, actor: str | None, mode: str | None = None, reason: str = "cancel-all"
) -> dict[str, int]:
    """Owner cancel-all (no kill): every active order, or those of one mode, under the
    approval lock(s). Returns {"cancelled": n, "requested": m}."""
    if mode is not None and mode not in MODES:
        raise BadRequest(f"unknown mode: {mode!r}")
    approval_locks(conn, mode)
    result = cancel_active_orders(conn, actor, reason, mode=mode)
    summary = {"cancelled": result["cancelled"], "requested": len(result["requested"])}
    add_audit(conn, "cancel_all", mode or "all", actor, None, {**summary, "reason": reason})
    return summary
