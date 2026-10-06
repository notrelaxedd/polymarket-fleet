"""Pinning: a machine whose Polymarket worker trades live money cannot be reassigned.

Reads the Polymarket tables (`jobs`, `assignments`, `orders`) read-only, as section 4.2
of docs/workloads-design.md defines. Pins are sticky; only a typed `unpin` clears one.
"""
from __future__ import annotations

from typing import Any

import psycopg

from host.errors import BadRequest, Conflict, NotFound
from host.events import add_audit

AUTO_REASON = "live trading"
MAX_REASON = 200
OPEN_ORDER_STATUSES = ("approved", "submitting", "open", "partial", "cancel_requested")
LIVE_TRADE_JOB = """
SELECT 1 FROM jobs j JOIN assignments a ON a.id::text = j.params->>'assignment_id'
 WHERE j.lease_worker_id = %(wid)s AND j.kind = 'trade' AND j.status IN ('leased', 'cancel_requested')
   AND a.mode = 'live' AND a.status IN ('active', 'halted')
"""
LIVE_OPEN_ORDER = """
SELECT 1 FROM orders o WHERE o.worker_id = %(wid)s AND o.mode = 'live' AND o.status = ANY(%(open)s)
"""


def _machine(conn: psycopg.Connection, machine_id: str, for_update: bool = False) -> dict[str, Any]:
    sql = "SELECT * FROM machines WHERE id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (machine_id,)).fetchone()
    if row is None:
        raise NotFound(f"unknown machine: {machine_id!r}")
    return row


def is_live_trading(conn: psycopg.Connection, machine_id: str) -> bool:
    """True when the machine's linked Polymarket worker holds a live trade job or an
    open live order."""
    row = conn.execute("SELECT polymarket_worker_id FROM machines WHERE id = %s", (machine_id,)).fetchone()
    worker_id = row["polymarket_worker_id"] if row else None
    if worker_id is None:
        return False
    if conn.execute(LIVE_TRADE_JOB + " LIMIT 1", {"wid": worker_id}).fetchone():
        return True
    return conn.execute(LIVE_OPEN_ORDER + " LIMIT 1", {"wid": worker_id, "open": list(OPEN_ORDER_STATUSES)}).fetchone() is not None


def _set_pin(conn: psycopg.Connection, machine_id: str, reason: str) -> dict[str, Any]:
    return conn.execute(
        "UPDATE machines SET pinned = true, pinned_reason = %s, pinned_at = now() WHERE id = %s RETURNING *",
        (reason, machine_id),
    ).fetchone()


def refresh_pins(conn: psycopg.Connection) -> list[str]:
    """Pin every unpinned machine that is trading live now; returns the ids newly pinned."""
    rows = conn.execute(
        "SELECT id FROM machines WHERE NOT pinned AND polymarket_worker_id IS NOT NULL ORDER BY id FOR UPDATE"
    ).fetchall()
    pinned: list[str] = []
    for row in rows:
        if not is_live_trading(conn, row["id"]):
            continue
        _set_pin(conn, row["id"], AUTO_REASON)
        add_audit(conn, "machine_pin", row["id"], "system", {"pinned": False}, {"pinned": True, "reason": AUTO_REASON})
        pinned.append(row["id"])
    return pinned


def pin(conn: psycopg.Connection, machine_id: str, reason: str, actor: str | None, ip: str | None) -> dict[str, Any]:
    """Pin a machine by hand (already pinned: unchanged). Audited."""
    machine = _machine(conn, machine_id, for_update=True)
    if machine["pinned"]:
        return machine
    text = (reason or "").strip()[:MAX_REASON] or "pinned by owner"
    row = _set_pin(conn, machine_id, text)
    add_audit(conn, "machine_pin", machine_id, actor, {"pinned": False}, {"pinned": True, "reason": text}, ip)
    return row


def unpin(conn: psycopg.Connection, machine_id: str, confirm: str, actor: str | None, ip: str | None) -> dict[str, Any]:
    """Clear a pin. `confirm` must be exactly "UNPIN <machine name>" (400); 409 while the
    machine is trading live. Audited with the typed phrase."""
    machine = _machine(conn, machine_id, for_update=True)
    phrase = f"UNPIN {machine['name']}"
    if confirm != phrase:
        raise BadRequest(f"type {phrase!r} to unpin this machine")
    if is_live_trading(conn, machine_id):
        raise Conflict("machine is trading live now; it cannot be unpinned")
    if not machine["pinned"]:
        return machine
    row = conn.execute(
        "UPDATE machines SET pinned = false, pinned_reason = NULL, pinned_at = NULL WHERE id = %s RETURNING *",
        (machine_id,),
    ).fetchone()
    add_audit(conn, "machine_unpin", machine_id, actor, {"pinned": True, "reason": machine["pinned_reason"]},
              {"pinned": False}, ip, confirmation_text=confirm)
    return row
