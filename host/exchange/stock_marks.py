"""The daily marks of the stock assignments (contract section 6), task stock_marks of
the exchange process.

Once per session, 30 minutes after its close (Alpaca's calendar, so half days are
right) at the earliest, every active or halted assignment created before the close
gets equity = cash + reserved + sum(qty * close) in stock_marks (idempotent, never
rewritten). The close is the session's daily bar from the feed, the official close the
backtest and the cls fills use. The feed fetches the day's bars at stock_bars_hour
(18:00 New York by default), so the mark waits (the task retries every minute) until
every held symbol has a complete bar for the session: a bar dated the session whose
symbol was fetched at least END_LAG after the close (an intraday fetch stores a
partial bar). An assignment with an order of the session still in flight (its fill
not booked yet) waits too. MARK_DEADLINE after the close the mark is written anyway
(a failed feed, a stuck order) with the fallbacks: Alpaca's current price of the
position, else the newest earlier bar. Then host.stocks.eligibility.recompute for the
models of the paper assignments marked.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg

from host.exchange.stock_bars import END_LAG
from host.exchange.stock_broker import IN_FLIGHT, NEW_YORK

log = logging.getLogger(__name__)

MARK_DELAY = timedelta(minutes=30)
MARK_DEADLINE = timedelta(hours=8)  # after the latest stock_bars_hour (23) on a normal day


def last_closed_session(calendar: list[dict[str, Any]], now: datetime) -> tuple[date, datetime] | None:
    """(date, close) of the newest trading day whose close + 30 minutes has passed."""
    best = None
    for day in calendar:
        try:
            d = date.fromisoformat(str(day["date"]))
            close = datetime.combine(d, dtime.fromisoformat(str(day["close"])[:5]), tzinfo=NEW_YORK)
        except (KeyError, ValueError):
            continue
        if close + MARK_DELAY <= now and (best is None or d > best[0]):
            best = (d, close)
    return best


def _closes(conn: psycopg.Connection, symbols: list[str], session: date) -> dict[str, dict[str, Any]]:
    """{symbol: {"close", "day", "fetched_at"}}: the newest daily bar dated on or before
    the session, with when the feed last fetched its symbol."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (b.symbol) b.symbol, b.close, (b.ts AT TIME ZONE 'America/New_York')::date AS day, i.fetched_at
          FROM stock_bars b LEFT JOIN instruments i ON i.symbol = b.symbol
         WHERE b.timeframe = '1Day' AND b.symbol = ANY(%s)
           AND (b.ts AT TIME ZONE 'America/New_York')::date <= %s ORDER BY b.symbol, b.ts DESC
        """,
        (symbols, session),
    ).fetchall()
    return {r["symbol"]: {"close": r["close"], "day": r["day"], "fetched_at": r["fetched_at"]} for r in rows}


def _assignments(conn: psycopg.Connection, session: date, close: datetime, wait_orders: bool) -> list[dict[str, Any]]:
    """The assignments to mark; with `wait_orders`, not those with an order of the
    session still in flight."""
    return conn.execute(
        """
        SELECT a.* FROM stock_assignments a WHERE a.status IN ('active', 'halted') AND a.created_at <= %(close)s
           AND NOT EXISTS (SELECT 1 FROM stock_marks m WHERE m.assignment_id = a.id AND m.session_date = %(session)s)
           AND NOT (%(wait)s AND EXISTS (SELECT 1 FROM stock_orders o WHERE o.assignment_id = a.id
                                          AND o.session_date = %(session)s AND o.status = ANY(%(busy)s)))
         ORDER BY a.id
        """,
        {"close": close, "session": session, "wait": wait_orders, "busy": list(IN_FLIGHT)},
    ).fetchall()


def mark_session(conn: psycopg.Connection, client: Any, session: date, close: datetime, now: datetime) -> dict[str, Any]:
    """Mark the assignments for `session` (see the module doc); then recompute the models
    of the paper assignments marked."""
    late = now >= close + MARK_DEADLINE
    rows = _assignments(conn, session, close, wait_orders=not late)
    if not rows:
        return {"session": session.isoformat(), "marked": 0}
    held = conn.execute("SELECT assignment_id, symbol, qty FROM stock_positions WHERE assignment_id = ANY(%s) AND qty > 0",
                        ([r["id"] for r in rows],)).fetchall()
    bars = _closes(conn, sorted({h["symbol"] for h in held}), session)
    prices = {s: Decimal(str(b["close"])) for s, b in bars.items()
              if b["day"] == session and b["fetched_at"] is not None and b["fetched_at"] >= close + END_LAG}
    sources = {s: "bar" for s in prices}
    missing = {h["symbol"] for h in held} - set(prices)
    if missing and not late:
        return {"session": session.isoformat(), "marked": 0, "waiting_for_bars": sorted(missing)}
    if missing:
        try:
            for p in client.positions():
                symbol, price = str(p.get("symbol")), p.get("current_price")
                if symbol in missing and price not in (None, ""):
                    prices[symbol], sources[symbol] = Decimal(str(price)), "alpaca"
        except Exception as exc:  # noqa: BLE001 - the older bar is the fallback
            log.warning("positions for the marks failed: %s", exc)
        for symbol in missing - set(prices):
            if symbol in bars:
                prices[symbol], sources[symbol] = Decimal(str(bars[symbol]["close"])), f"bar {bars[symbol]['day']}"
    marked, models = 0, set()
    for a in rows:
        value = sum((Decimal(int(h["qty"])) * prices.get(h["symbol"], Decimal(0)) * 100 for h in held
                     if h["assignment_id"] == a["id"]), Decimal(0))
        positions = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        equity = int(a["cash_cents"]) + int(a["reserved_cents"]) + positions
        done = conn.execute("INSERT INTO stock_marks (assignment_id, session_date, equity_cents, positions_cents)"
                            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1", (a["id"], session, equity, positions)).fetchone()
        if done:
            marked += 1
            if a["mode"] == "paper":
                models.add(int(a["model_id"]))
    from host.stocks import eligibility  # builder B's module, imported when first needed

    for model_id in sorted(models):
        eligibility.recompute(conn, model_id)
    return {"session": session.isoformat(), "marked": marked, "models_recomputed": sorted(models), "price_sources": sources}


class Marker:
    """Holds the Alpaca calendar between runs (one request per New York day)."""

    def __init__(self) -> None:
        self.calendar: list[dict[str, Any]] = []
        self.calendar_day: date | None = None

    def run(self, conn: psycopg.Connection, client: Any, now: datetime) -> dict[str, Any]:
        today = now.astimezone(NEW_YORK).date()
        if self.calendar_day != today:
            self.calendar = client.calendar(today - timedelta(days=10), today)
            self.calendar_day = today
        found = last_closed_session(self.calendar, now)
        if found is None:
            return {"session": None, "marked": 0}
        return mark_session(conn, client, *found, now)
