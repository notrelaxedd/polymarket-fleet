"""Stock splits applied to our books (run by the stock_broker task of the exchange
process, before the reconciliation).

The bar feed stores split-adjusted history and Alpaca adjusts the account's positions
on the ex date, so stock_positions must follow, or equity, the limits, the marks and
the reconciliation all go wrong. Once per New York day the watcher reads the forward
and reverse splits of the held symbols (Alpaca's corporate actions, the last
LOOKBACK days and the next week) and applies every split whose ex date has come,
from APPLY_AT New York on the ex date (the session open: Alpaca has adjusted the
account by then), once:

- for every assignment (both modes: the paper account splits too), the shares held
  before the ex date (the position less the fills booked since) become
  floor(qty * new_rate / old_rate); shares bought after it are already new shares.
  cost_cents stays (a split does not change the cost basis), less the cost share of a
  fractional remainder, which goes to cash as cash in lieu at its cost (no realized
  P&L; Alpaca's actual cash in lieu may differ by cents and is not reconciled).
- the symbol's instruments.fetched_at is cleared, so the feed fetches the adjusted
  history again; until it has, the bars do not reach the previous session for the
  host and no decision is due for an assignment holding the symbol.
- an audit row `stock_split_applied` with entity "stock_split:<symbol>:<ex date>" is
  the record that it was applied (taken under an advisory lock, never applied twice).

On the ex date before APPLY_AT the symbol is `pending`: the reconciliation does not
compare it (Alpaca may already show the new shares). A split the watcher could not
read is not applied: the reconciliation then sees the difference (live auto-kills).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import psycopg

from host.events import add_audit
from host.exchange.stock_broker import NEW_YORK

log = logging.getLogger(__name__)

ACTION = "stock_split_applied"
ACTOR = "exchange"
LOOKBACK = timedelta(days=30)
LOOKAHEAD = timedelta(days=7)
APPLY_AT = dtime(9, 30)
RETRY_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class Split:
    symbol: str
    ex_date: date
    old_rate: Decimal
    new_rate: Decimal

    @property
    def entity(self) -> str:
        return f"stock_split:{self.symbol}:{self.ex_date.isoformat()}"


def parse_split(item: dict[str, Any]) -> Split | None:
    try:
        split = Split(str(item["symbol"]), date.fromisoformat(str(item["ex_date"])[:10]),
                      Decimal(str(item["old_rate"])), Decimal(str(item["new_rate"])))
    except (KeyError, ValueError, InvalidOperation):
        return None
    return split if split.old_rate > 0 and split.new_rate > 0 and split.old_rate != split.new_rate else None


def applied(conn: psycopg.Connection, splits: list[Split]) -> set[str]:
    rows = conn.execute("SELECT entity FROM audit_log WHERE action = %s AND entity = ANY(%s)",
                        (ACTION, [s.entity for s in splits])).fetchall() if splits else []
    return {r["entity"] for r in rows}


def apply_split(conn: psycopg.Connection, split: Split, now: datetime) -> dict[str, Any] | None:
    """Apply one split to every assignment's position (see the module doc); the audit
    detail, or None when it was applied before."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (split.entity,))
    if applied(conn, [split]):
        return None
    since = datetime.combine(split.ex_date, dtime(0), tzinfo=NEW_YORK)
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM stock_assignments WHERE id IN (SELECT assignment_id FROM stock_positions WHERE symbol = %s)"
        " ORDER BY id FOR UPDATE", (split.symbol,)).fetchall()]
    changes = []
    for aid in ids:
        pos = conn.execute("SELECT qty, cost_cents FROM stock_positions WHERE assignment_id = %s AND symbol = %s FOR UPDATE",
                           (aid, split.symbol)).fetchone()
        after = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN o.side = 'buy' THEN f.qty ELSE -f.qty END), 0) AS n FROM stock_fills f"
            " JOIN stock_orders o ON o.id = f.order_id WHERE o.assignment_id = %s AND o.symbol = %s AND f.ts >= %s",
            (aid, split.symbol, since)).fetchone()["n"]
        before = int(pos["qty"]) - int(after)
        if before <= 0:
            continue
        exact = Decimal(before) * split.new_rate / split.old_rate
        whole = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        fraction = exact - whole
        lieu = int((Decimal(int(pos["cost_cents"])) * fraction / (exact + int(after))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        qty = whole + int(after)
        conn.execute("UPDATE stock_positions SET qty = %s, cost_cents = cost_cents - %s, updated_at = now()"
                     " WHERE assignment_id = %s AND symbol = %s", (qty, lieu, aid, split.symbol))
        if lieu:
            conn.execute("UPDATE stock_assignments SET cash_cents = cash_cents + %s, updated_at = now() WHERE id = %s", (lieu, aid))
        changes.append({"assignment_id": aid, "qty_before": int(pos["qty"]), "qty_after": qty, "split_shares": before,
                        "fraction": str(fraction), "cash_in_lieu_cents": lieu})
    conn.execute("UPDATE instruments SET fetched_at = NULL WHERE symbol = %s", (split.symbol,))
    detail = {"symbol": split.symbol, "ex_date": split.ex_date.isoformat(), "old_rate": str(split.old_rate),
              "new_rate": str(split.new_rate), "applied_at": now.isoformat(), "assignments": changes}
    add_audit(conn, ACTION, split.entity, ACTOR, None, detail)
    log.warning("stock split applied: %s", detail)
    return detail


class SplitWatcher:
    """Holds the day's splits between broker checks (one corporate-actions read a day)."""

    def __init__(self) -> None:
        self.day: date | None = None
        self.next_try: datetime | None = None
        self.splits: list[Split] = []

    def _read(self, conn: psycopg.Connection, data: Any, now: datetime) -> str | None:
        today = now.astimezone(NEW_YORK).date()
        if self.day == today or (self.next_try is not None and now < self.next_try):
            return None
        held = [r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM stock_positions WHERE qty > 0 ORDER BY symbol")]
        try:
            items = data.splits(held, today - LOOKBACK, today + LOOKAHEAD) if held else []
        except Exception as exc:  # noqa: BLE001 - retried after RETRY_AFTER, reported by the task
            self.next_try = now + RETRY_AFTER
            return f"corporate actions: {exc}"[:300]
        self.splits = sorted({s for s in map(parse_split, items) if s is not None and s.symbol in held},
                             key=lambda s: (s.ex_date, s.symbol))
        self.day, self.next_try = today, None
        return None

    def run(self, conn: psycopg.Connection, data: Any, now: datetime) -> dict[str, Any]:
        """{"applied": [...], "pending": {symbol}, "error"}: the splits due applied."""
        if data is None:
            return {"applied": [], "pending": set(), "error": None}
        error = self._read(conn, data, now)
        local = now.astimezone(NEW_YORK)
        today, opened = local.date(), local.time() >= APPLY_AT
        known = [s for s in self.splits if s.ex_date <= today]
        done = applied(conn, known)
        out: list[dict[str, Any]] = []
        pending: set[str] = set()
        for split in known:
            if split.entity in done:
                continue
            if split.ex_date == today and not opened:
                pending.add(split.symbol)
                continue
            detail = apply_split(conn, split, now)
            if detail is not None:
                out.append(detail)
        return {"applied": out, "pending": pending, "error": error}
