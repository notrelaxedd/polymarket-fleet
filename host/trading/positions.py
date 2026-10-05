"""Open positions, mark-to-mid valuation, the owner's day and losses_today.

A position is the signed sum of an assignment's fills on a market that is not resolved
yet: buys add their size and `fills.basis_cents`, sells subtract theirs (the basis a
sale removed, docs/TRADING.md "Selling"), so `size = buys - sells`, `basis_cents =
sum(buy basis) - sum(sell basis)` and `avg_cost = basis_cents / (size * 100)`. A
position sold down to zero disappears; once the market resolves the ledger `settle` row
moves the money to realized and the position disappears too. The mark is the mid of
the market's latest snapshot (fallback: the best bid/ask mirrored on the market row; no
mark at all means mark = basis).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

from host.settings import get_setting
from host.trading import ledger

SIGNED_SIZE = "CASE WHEN o.side = 'sell' THEN -f.size ELSE f.size END"
SIGNED_BASIS = "CASE WHEN o.side = 'sell' THEN -1 ELSE 1 END * COALESCE(f.basis_cents, ROUND(f.price * f.size * 100))"

UNRESOLVED_SQL = f"""
    SELECT o.market_id, m.side, SUM({SIGNED_SIZE}) AS size, SUM({SIGNED_BASIS}) AS basis_cents
      FROM fills f
      JOIN orders o ON o.id = f.order_id
      JOIN markets m ON m.id = o.market_id
     WHERE m.status <> 'resolved' AND {{where}}
     GROUP BY o.market_id, m.side
     ORDER BY o.market_id
"""


def server_now(conn: psycopg.Connection) -> datetime:
    """Database clock, UTC."""
    return conn.execute("SELECT now() AS t").fetchone()["t"].astimezone(timezone.utc)


def owner_tz(conn: psycopg.Connection) -> ZoneInfo:
    """settings.tz as a ZoneInfo (UTC when the stored name is unusable)."""
    name = get_setting(conn, "tz", "UTC")
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo("UTC")


def owner_day(conn: psycopg.Connection, now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the owner's calendar day containing `now` (default: the DB clock)."""
    tz = owner_tz(conn)
    moment = (now or server_now(conn)).astimezone(tz)
    start = datetime.combine(moment.date(), time.min, tzinfo=tz)
    end = datetime.combine(moment.date() + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def _mark_mid(conn: psycopg.Connection, market_id: Any) -> float | None:
    """Mid of the latest snapshot, else the mid of the market's mirrored bid/ask."""
    snap = conn.execute(
        "SELECT mid, bid, ask FROM price_snapshots WHERE market_id = %s ORDER BY ts DESC, id DESC LIMIT 1",
        (market_id,),
    ).fetchone()
    if snap is not None and snap["mid"] is not None:
        return float(snap["mid"])
    if snap is not None and snap["bid"] is not None and snap["ask"] is not None:
        return (float(snap["bid"]) + float(snap["ask"])) / 2
    market = conn.execute("SELECT best_bid, best_ask FROM markets WHERE id = %s", (market_id,)).fetchone()
    if market is not None and market["best_bid"] is not None and market["best_ask"] is not None:
        return (float(market["best_bid"]) + float(market["best_ask"])) / 2
    return None


def _rows_to_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        size = int(row["size"] or 0)
        basis = int(row["basis_cents"] or 0)
        if size <= 0:
            continue
        out.append(
            {
                "market_id": row["market_id"],
                "side": row["side"],
                "size": size,
                "basis_cents": basis,
                "avg_cost": round(basis / (size * 100), 6),
            }
        )
    return out


def held(conn: psycopg.Connection, assignment_id: Any, market_id: Any) -> tuple[int, int]:
    """(size, basis_cents) the assignment holds on one market, signed over every fill
    whatever the market's status (what a sell fill draws its basis from)."""
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM({SIGNED_SIZE}), 0) AS size, COALESCE(SUM({SIGNED_BASIS}), 0) AS basis_cents
          FROM fills f JOIN orders o ON o.id = f.order_id
         WHERE o.assignment_id = %s AND o.market_id = %s
        """,
        (assignment_id, market_id),
    ).fetchone()
    return int(row["size"]), int(row["basis_cents"])


def sell_basis_cents(held_size: int, held_basis: int, size: int) -> int:
    """The basis a sale of `size` contracts removes from a position of `held_size`
    contracts with `held_basis` cents of basis (average cost): held_basis * size /
    held_size rounded half up, and exactly the remaining basis when the sale closes
    the position."""
    if size <= 0 or held_size <= 0:
        return 0
    if size >= held_size:
        return int(held_basis)
    if held_basis <= 0:
        return 0
    return (2 * int(held_basis) * int(size) + int(held_size)) // (2 * int(held_size))


def positions(conn: psycopg.Connection, assignment_id: Any) -> list[dict[str, Any]]:
    """Open positions of one assignment: {market_id, side, size, basis_cents, avg_cost}."""
    rows = conn.execute(UNRESOLVED_SQL.format(where="o.assignment_id = %s"), (assignment_id,)).fetchall()
    return _rows_to_positions(rows)


def positions_for_mode(conn: psycopg.Connection, mode: str) -> list[dict[str, Any]]:
    """Open positions pooled per market across every assignment of a mode."""
    rows = conn.execute(UNRESOLVED_SQL.format(where="o.mode = %s"), (mode,)).fetchall()
    return _rows_to_positions(rows)


def mark_positions(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add `mark_cents` (mid * size * 100) and `unrealized_cents` to position rows."""
    for row in rows:
        mid = _mark_mid(conn, row["market_id"])
        mark = row["basis_cents"] if mid is None else int(round(mid * row["size"] * 100))
        row["mark_cents"] = mark
        row["unrealized_cents"] = mark - row["basis_cents"]
    return rows


def unrealized_cents(conn: psycopg.Connection, mode: str) -> int:
    """Mark-to-mid gain (negative: loss) over every open position of a mode."""
    return sum(r["unrealized_cents"] for r in mark_positions(conn, positions_for_mode(conn, mode)))


def reserved_cents(conn: psycopg.Connection, mode: str) -> int:
    """Cents reserved by still-open orders of a mode (the cached bankroll column)."""
    row = conn.execute("SELECT COALESCE(SUM(reserved_cents), 0) AS s FROM bankrolls WHERE mode = %s", (mode,)).fetchone()
    return int(row["s"])


def exposure_cents(conn: psycopg.Connection, mode: str) -> int:
    """Open order reservations plus the basis of open positions for a mode."""
    row = conn.execute(
        "SELECT COALESCE(SUM(reserved_cents + open_cost_cents), 0) AS s FROM bankrolls WHERE mode = %s", (mode,)
    ).fetchone()
    return int(row["s"])


def losses_today(conn: psycopg.Connection, mode: str, now: datetime | None = None) -> dict[str, int]:
    """Today's losses of a mode in the owner's day: realized (ledger d_realized in
    [day start, day end)) plus the mark-to-mid of open positions; `losses_cents =
    max(0, -(realized + unrealized))`. `reserved_cents` (the cost of still-open orders)
    rides along for the approval check."""
    start, end = owner_day(conn, now)
    realized = ledger.realized_between(conn, mode, start, end)
    unrealized = unrealized_cents(conn, mode)
    return {
        "realized_cents": realized,
        "unrealized_cents": unrealized,
        "losses_cents": max(0, -(realized + unrealized)),
        "reserved_cents": reserved_cents(conn, mode),
    }
