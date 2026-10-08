"""The broker side of stocks on Alpaca (contract section 6): the account and the clock
into stock_broker_state and the reconciliation of positions and open orders (the daily
marks are host/exchange/stock_marks.py, the splits host/exchange/stock_splits.py).

- `check`: account + clock -> stock_broker_state (environment from the credentials,
  cents from Alpaca's dollar strings rounded half up, session_date = the New York date
  of next_close while the market is open, else of next_open). A failure keeps the
  last good values and checked_at (so approvals see the row go stale) and records
  last_error.
- `reconcile`: for the mode equal to the environment, the sum of stock_positions over
  its assignments against Alpaca's positions, and Alpaca's open orders that are not
  ours, into `warnings`. A symbol with one of our orders in flight is not compared
  (its fill may be booked a poll later), nor one with a split not applied yet. An open
  order is ours only while its row is in flight in the keys' mode (a row closed here
  whose order is still open at Alpaca would fill unbooked), and a smoke order
  (SMOKE_PREFIX) only for SMOKE_MAX_AGE. Live also auto-kills: `unknown_order` at once,
  `stock_position_mismatch` when the same mismatch is seen on two checks in a row.
  Active rows of the other mode (another account, untracked until its keys return)
  get a warning.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from host.exchange.live_sync import maybe_auto_kill

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
SMOKE_PREFIX = "fleet-smoke-"
SMOKE_MAX_AGE = timedelta(minutes=15)  # the smoke's hold and cancel retries, with room
WARNING_TTL = timedelta(hours=24)
RECONCILE_KINDS = ("position_mismatch", "unknown_order", "other_mode_orders")
IN_FLIGHT = ("submitting", "open", "partial", "cancel_requested")


def cents_of(value: Any) -> int | None:
    """Alpaca's dollar string in whole cents, rounded half up (None when absent)."""
    if value is None or value == "":
        return None
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


def qty_of(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal(0)


def parse_ts(value: Any) -> datetime | None:
    """An Alpaca timestamp (offset, optional nanoseconds) as an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest[len(digits):]}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def session_of(clock: dict[str, Any]) -> date | None:
    moment = parse_ts(clock.get("next_close") if clock.get("is_open") else clock.get("next_open"))
    return moment.astimezone(NEW_YORK).date() if moment else None


# ------------------------------------------------------------------- warnings

def _warnings(conn: psycopg.Connection) -> list[dict[str, Any]]:
    row = conn.execute("SELECT warnings FROM stock_broker_state WHERE id = 1 FOR UPDATE").fetchone()
    return [w for w in (row["warnings"] if row and isinstance(row["warnings"], list) else []) if isinstance(w, dict)]


def _fresh(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now - WARNING_TTL
    return [w for w in items if (parse_ts(w.get("ts")) or now) >= cutoff]


def add_warning(conn: psycopg.Connection, kind: str, message: str, detail: dict[str, Any] | None = None,
                now: datetime | None = None) -> None:
    """Append one warning to the broker row (kept 24 hours; the /stocks page shows them)."""
    now = now or datetime.now(timezone.utc)
    items = _fresh(_warnings(conn), now) + [{"kind": kind, "message": message, "ts": now.isoformat(),
                                             "detail": {k: str(v) for k, v in (detail or {}).items()}}]
    conn.execute("UPDATE stock_broker_state SET warnings = %s WHERE id = 1", (Jsonb(items[-50:]),))


# ---------------------------------------------------------------------- check

def mark_keys_absent(conn: psycopg.Connection, error: str | None) -> None:
    """No keys (or a bad ALPACA_BASE_URL): say so on the row, touch nothing else."""
    conn.execute("UPDATE stock_broker_state SET keys_present = false, last_error = %s WHERE id = 1"
                 " AND (keys_present OR last_error IS DISTINCT FROM %s)", (error, error))


def check(conn: psycopg.Connection, client: Any, now: datetime) -> dict[str, Any]:
    """Account and clock into stock_broker_state; the stored fields, or {"error"}."""
    try:
        account, clock = client.account(), client.clock()
    except Exception as exc:  # noqa: BLE001 - recorded, retried next poll
        error = str(exc)[:500] or exc.__class__.__name__
        conn.execute("UPDATE stock_broker_state SET environment = %s, keys_present = true, last_error = %s WHERE id = 1",
                     (client.environment, error))
        return {"error": error}
    values = {
        "environment": client.environment, "keys_present": True, "account_status": account.get("status"),
        "equity_cents": cents_of(account.get("equity")), "cash_cents": cents_of(account.get("cash")),
        "buying_power_cents": cents_of(account.get("buying_power")),
        "pattern_day_trader": bool(account.get("pattern_day_trader")) if account.get("pattern_day_trader") is not None else None,
        "daytrade_count": int(account["daytrade_count"]) if isinstance(account.get("daytrade_count"), int) else None,
        "market_open": bool(clock.get("is_open")), "session_date": session_of(clock),
        "next_open": parse_ts(clock.get("next_open")), "next_close": parse_ts(clock.get("next_close")),
        "checked_at": now, "last_error": None,
    }
    conn.execute("UPDATE stock_broker_state SET " + ", ".join(f"{k} = %s" for k in values) + " WHERE id = 1",
                 list(values.values()))
    return values


# ------------------------------------------------------------------ reconcile

def ours(conn: psycopg.Connection, mode: str) -> tuple[dict[str, int], set[str]]:
    """({symbol: summed qty} of the mode's positions, symbols with an order in flight),
    read in one statement so both come from the same snapshot."""
    row = conn.execute(
        """
        SELECT (SELECT COALESCE(jsonb_object_agg(symbol, q), '{}') FROM (
                  SELECT p.symbol, SUM(p.qty) AS q FROM stock_positions p
                    JOIN stock_assignments a ON a.id = p.assignment_id
                   WHERE a.mode = %(mode)s GROUP BY p.symbol HAVING SUM(p.qty) <> 0) s) AS held,
               (SELECT COALESCE(array_agg(DISTINCT symbol), '{}') FROM stock_orders
                 WHERE mode = %(mode)s AND status = ANY(%(busy)s)) AS busy
        """,
        {"mode": mode, "busy": list(IN_FLIGHT)},
    ).fetchone()
    return {k: int(v) for k, v in (row["held"] or {}).items()}, set(row["busy"] or [])


def known_order_ids(conn: psycopg.Connection, client_ids: list[str], mode: str) -> set[str]:
    """The client ids that are rows of `mode` in flight here."""
    ids = []
    for cid in client_ids:
        try:
            ids.append(uuid.UUID(cid))
        except (ValueError, TypeError):
            continue
    rows = conn.execute("SELECT id FROM stock_orders WHERE id = ANY(%s) AND mode = %s AND status = ANY(%s)",
                        (ids, mode, list(IN_FLIGHT))).fetchall() if ids else []
    return {str(r["id"]) for r in rows}


def young_smoke(order: dict[str, Any], cid: str, now: datetime) -> bool:
    """A stock-smoke order still inside its hold (an older one is not ours any more)."""
    created = parse_ts(order.get("created_at")) or parse_ts(order.get("submitted_at"))
    return cid.startswith(SMOKE_PREFIX) and created is not None and now - created < SMOKE_MAX_AGE


def other_mode_active(conn: psycopg.Connection, mode: str) -> int:
    row = conn.execute("SELECT count(*) AS n FROM stock_orders WHERE mode <> %s AND status = ANY(%s)",
                       (mode, list(IN_FLIGHT))).fetchone()
    return int(row["n"])


class Reconciler:
    """Holds the mismatches seen on the previous check (the live debounce)."""

    def __init__(self) -> None:
        self.previous: set[tuple[str, int, str]] = set()

    def run(self, conn: psycopg.Connection, client: Any, now: datetime, skip: frozenset[str] = frozenset()) -> dict[str, Any]:
        """`skip`: symbols not compared (a split not applied to our books yet)."""
        mode = client.environment
        held, busy = ours(conn, mode)
        busy |= set(skip)
        remote = {str(p.get("symbol")): qty_of(p.get("qty")) for p in client.positions()}
        mismatches = []
        for symbol in sorted(set(held) | set(remote)):
            mine, theirs = held.get(symbol, 0), remote.get(symbol, Decimal(0))
            if Decimal(mine) != theirs and symbol not in busy:
                mismatches.append({"symbol": symbol, "ours": mine, "alpaca": str(theirs)})
        open_orders = client.orders("open")
        ids = [str(o.get("client_order_id") or "") for o in open_orders]
        known = known_order_ids(conn, ids, mode)
        unknown = [{"exchange_order_id": o.get("id"), "client_order_id": cid, "symbol": o.get("symbol"), "side": o.get("side"),
                    "qty": o.get("qty")} for o, cid in zip(open_orders, ids) if cid not in known and not young_smoke(o, cid, now)]
        others = other_mode_active(conn, mode)
        stamp = now.isoformat()
        items = [w for w in _fresh(_warnings(conn), now) if w.get("kind") not in RECONCILE_KINDS]
        items += [{"kind": "position_mismatch", "ts": stamp, "detail": m,
                   "message": f"{m['symbol']}: our {mode} positions hold {m['ours']}, Alpaca holds {m['alpaca']}"} for m in mismatches]
        items += [{"kind": "unknown_order", "ts": stamp, "detail": {k: str(v) for k, v in u.items()},
                   "message": f"open order at Alpaca that is not ours: {u['side']} {u['qty']} {u['symbol']}"} for u in unknown]
        if others:
            other = "paper" if mode == "live" else "live"
            items.append({"kind": "other_mode_orders", "ts": stamp, "detail": {"count": str(others)},
                          "message": f"{others} {other} order(s) are active but the keys are {mode}: they are not tracked"
                                     f" until {other} keys return"})
        conn.execute("UPDATE stock_broker_state SET warnings = %s WHERE id = 1", (Jsonb(items[-50:]),))
        seen = {(m["symbol"], m["ours"], m["alpaca"]) for m in mismatches}
        killed = None
        if mode == "live":
            repeated = [m for m in mismatches if (m["symbol"], m["ours"], m["alpaca"]) in self.previous]
            if unknown and maybe_auto_kill(conn, "unknown_order", {"orders": unknown[:10], "count": len(unknown)}):
                killed = "unknown_order"
            if repeated and maybe_auto_kill(conn, "stock_position_mismatch", {"mismatches": repeated[:20]}):
                killed = killed or "stock_position_mismatch"
        self.previous = seen
        return {"mismatches": mismatches, "unknown_orders": unknown, "other_mode_orders": others, "auto_killed": killed}
