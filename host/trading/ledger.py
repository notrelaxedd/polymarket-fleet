"""Append-only money ledger and the cached bankroll columns.

Every money movement is one signed ledger row; `bankrolls` caches the running sums and
`replay_problems` proves the cache matches. Conventions (cents) are in docs/TRADING.md
(kinds fund, reserve, release, fill, sell, settle, adjust).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from host.errors import Conflict, NotFound


class LedgerError(Conflict):
    """A movement that would make a bankroll column negative."""


def get_bankroll(conn: psycopg.Connection, bankroll_id: Any, for_update: bool = False) -> dict[str, Any]:
    sql = "SELECT * FROM bankrolls WHERE id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (bankroll_id,)).fetchone()
    if row is None:
        raise NotFound(f"unknown bankroll {bankroll_id}")
    return dict(row)


def bankroll_for_assignment(conn: psycopg.Connection, assignment_id: Any, for_update: bool = False) -> dict[str, Any]:
    sql = "SELECT * FROM bankrolls WHERE assignment_id = %s" + (" FOR UPDATE" if for_update else "")
    row = conn.execute(sql, (assignment_id,)).fetchone()
    if row is None:
        raise NotFound(f"no bankroll for assignment {assignment_id}")
    return dict(row)


def create_bankroll(conn: psycopg.Connection, assignment_id: Any, mode: str, initial_cents: int) -> dict[str, Any]:
    """Insert a bankroll with zero balances, then post the `fund` row."""
    if initial_cents < 0:
        raise LedgerError("initial bankroll must not be negative")
    row = conn.execute(
        """
        INSERT INTO bankrolls (assignment_id, mode, initial_cents, available_cents)
        VALUES (%s, %s, %s, 0) RETURNING *
        """,
        (assignment_id, mode, initial_cents),
    ).fetchone()
    fund(conn, row["id"], initial_cents, ref_id=str(assignment_id))
    return get_bankroll(conn, row["id"])


def _post(
    conn: psycopg.Connection,
    bankroll_id: Any,
    kind: str,
    *,
    d_available: int = 0,
    d_reserved: int = 0,
    d_open: int = 0,
    d_realized: int = 0,
    ref_type: str | None = None,
    ref_id: Any = None,
    note: str | None = None,
) -> int:
    """Lock the bankroll, refuse negative results, append the row, update the cache."""
    bank = get_bankroll(conn, bankroll_id, for_update=True)
    new_available = bank["available_cents"] + d_available
    new_reserved = bank["reserved_cents"] + d_reserved
    new_open = bank["open_cost_cents"] + d_open
    if new_available < 0 or new_reserved < 0 or new_open < 0:
        raise LedgerError(
            f"{kind} of {d_available}/{d_reserved}/{d_open} would overdraw bankroll {bankroll_id}"
        )
    row = conn.execute(
        """
        INSERT INTO ledger (bankroll_id, mode, kind, d_available, d_reserved, d_open, d_realized,
                            ref_type, ref_id, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (bankroll_id, bank["mode"], kind, d_available, d_reserved, d_open, d_realized, ref_type,
         None if ref_id is None else str(ref_id), note),
    ).fetchone()
    conn.execute(
        """
        UPDATE bankrolls SET available_cents = %s, reserved_cents = %s, open_cost_cents = %s,
               realized_pnl_cents = realized_pnl_cents + %s, updated_at = now()
         WHERE id = %s
        """,
        (new_available, new_reserved, new_open, d_realized, bankroll_id),
    )
    return int(row["id"])


def fund(conn: psycopg.Connection, bankroll_id: Any, cents: int, ref_id: Any = None) -> int:
    """`+cents available` (initial funding or a top-up)."""
    return _post(conn, bankroll_id, "fund", d_available=cents, ref_type="assignment", ref_id=ref_id)


def reserve(conn: psycopg.Connection, bankroll_id: Any, cents: int, order_id: Any) -> int:
    """Approval: `-cents available, +cents reserved`."""
    return _post(conn, bankroll_id, "reserve", d_available=-cents, d_reserved=cents, ref_type="order", ref_id=order_id)


def release(conn: psycopg.Connection, bankroll_id: Any, cents: int, order_id: Any, note: str | None = None) -> int:
    """Cancel, expiry or rejection: the unfilled reservation goes back: `+cents available, -cents reserved`."""
    if cents <= 0:
        return 0
    return _post(conn, bankroll_id, "release", d_available=cents, d_reserved=-cents, ref_type="order", ref_id=order_id, note=note)


def fill(conn: psycopg.Connection, bankroll_id: Any, cost_cents: int, fee_cents: int, order_id: Any) -> int:
    """A fill: `-(cost+fee) reserved, +cost open, -fee realized`; cost = price * size * 100."""
    return _post(
        conn, bankroll_id, "fill",
        d_reserved=-(cost_cents + fee_cents), d_open=cost_cents, d_realized=-fee_cents,
        ref_type="order", ref_id=order_id,
    )


def sell(conn: psycopg.Connection, bankroll_id: Any, basis_cents: int, proceeds_cents: int, fee_cents: int, order_id: Any) -> int:
    """A sell fill: `-basis open, +(proceeds - fee) available, +(proceeds - fee - basis)
    realized`; proceeds = price * size * 100, basis = the position basis the sale removes
    (host.trading.positions.sell_basis_cents). Nothing was reserved, so nothing is
    released."""
    if basis_cents < 0 or proceeds_cents < 0 or fee_cents < 0:
        raise LedgerError("a sale needs a non-negative basis, proceeds and fee")
    net = proceeds_cents - fee_cents
    return _post(
        conn, bankroll_id, "sell",
        d_open=-basis_cents, d_available=net, d_realized=net - basis_cents,
        ref_type="order", ref_id=order_id,
    )


def settle(conn: psycopg.Connection, bankroll_id: Any, basis_cents: int, payout_cents: int, assignment_id: Any) -> int:
    """Resolution: `-basis open, +payout available, +(payout - basis) realized`."""
    return _post(
        conn, bankroll_id, "settle",
        d_open=-basis_cents, d_available=payout_cents, d_realized=payout_cents - basis_cents,
        ref_type="assignment", ref_id=assignment_id,
    )


def adjust(conn: psycopg.Connection, bankroll_id: Any, d_available: int, note: str, actor: str | None) -> int:
    """Owner correction of the available balance; `note` is required."""
    if not note:
        raise LedgerError("an adjustment needs a note")
    return _post(conn, bankroll_id, "adjust", d_available=d_available, d_realized=d_available, ref_type="owner", ref_id=actor, note=note)


def replay(conn: psycopg.Connection, bankroll_id: Any) -> dict[str, int]:
    """Column sums recomputed from the ledger."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(d_available), 0) AS available_cents, COALESCE(SUM(d_reserved), 0) AS reserved_cents,
               COALESCE(SUM(d_open), 0) AS open_cost_cents, COALESCE(SUM(d_realized), 0) AS realized_pnl_cents
          FROM ledger WHERE bankroll_id = %s
        """,
        (bankroll_id,),
    ).fetchone()
    return {k: int(v) for k, v in row.items()}


def replay_problems(conn: psycopg.Connection, bankroll_id: Any = None) -> list[str]:
    """Bankrolls whose cached columns disagree with their ledger, or whose identity
    `initial + realized = available + reserved + open_cost` fails."""
    if bankroll_id is None:
        ids = [r["id"] for r in conn.execute("SELECT id FROM bankrolls ORDER BY id").fetchall()]
    else:
        ids = [bankroll_id]
    problems = []
    for bid in ids:
        bank = get_bankroll(conn, bid)
        sums = replay(conn, bid)
        for col in ("available_cents", "reserved_cents", "open_cost_cents", "realized_pnl_cents"):
            if int(bank[col]) != sums[col]:
                problems.append(f"bankroll {bid}: {col} cached {bank[col]} but ledger sums to {sums[col]}")
        lhs = int(bank["initial_cents"]) + int(bank["realized_pnl_cents"])
        rhs = int(bank["available_cents"]) + int(bank["reserved_cents"]) + int(bank["open_cost_cents"])
        if lhs != rhs:
            problems.append(f"bankroll {bid}: initial + realized = {lhs} but available + reserved + open = {rhs}")
    return problems


def realized_between(conn: psycopg.Connection, mode: str, start: datetime, end: datetime) -> int:
    """Sum of realized movements for a mode in [start, end)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(d_realized), 0) AS s FROM ledger WHERE mode = %s AND ts >= %s AND ts < %s",
        (mode, start, end),
    ).fetchone()
    return int(row["s"])
