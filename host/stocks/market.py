"""What the host knows about the market: the broker row the exchange process writes
(stock_broker_state), the reference prices from the daily bars, and whether the bars
reach the previous session.

A reference price is the adjusted close of the newest 1Day bar dated (New York) before
the session, in whole cents rounded half up: the price a decision and its limits use,
the same close the backtest trades at. Bars come only from the exchange process's feed.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from host.settings import get_int_setting

NEW_YORK = ZoneInfo("America/New_York")
STALE_POLLS = 4  # a broker check older than 4 * stock_broker_poll_s is stale
MOC_CUTOFF = timedelta(minutes=11)  # Alpaca refuses cls orders after 15:50 New York
FEED_AFTER_CLOSE = dtime(16, 30)  # a feed run after this saw the day's bar if there was one
BROKER_FIELDS = ("environment", "keys_present", "account_status", "equity_cents", "cash_cents", "buying_power_cents",
                 "market_open", "session_date", "next_open", "next_close", "checked_at", "last_error", "warnings")


def cents(price: Any) -> int:
    """Dollars to whole cents, rounded half up."""
    return int((Decimal(str(price)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def broker_state(conn: psycopg.Connection) -> dict[str, Any]:
    """The single stock_broker_state row ({} fields as None when it is missing)."""
    row = conn.execute("SELECT * FROM stock_broker_state WHERE id = 1").fetchone()
    return {k: (row or {}).get(k) for k in BROKER_FIELDS}


def max_broker_age_s(conn: psycopg.Connection) -> int:
    return STALE_POLLS * get_int_setting(conn, "stock_broker_poll_s", 30)


def broker_stale(broker: dict[str, Any], now: datetime, max_age_s: int) -> bool:
    checked = broker.get("checked_at")
    return checked is None or (now - checked).total_seconds() > max_age_s


def broker_problem(conn: psycopg.Connection, broker: dict[str, Any], mode: str, now: datetime) -> str | None:
    """Why `mode` cannot trade on the broker as last checked (None when it can)."""
    if not broker.get("keys_present"):
        return "the exchange process has no Alpaca keys (exchange.env)"
    if broker.get("environment") != mode:
        return f"the Alpaca keys are {broker.get('environment') or 'unknown'} keys, not {mode}"
    max_age = max_broker_age_s(conn)
    if broker_stale(broker, now, max_age):
        return f"the last broker check is older than {max_age} s (is the exchange process running?)"
    return None


def ref_prices(conn: psycopg.Connection, symbols: list[str], session_date: date | None) -> dict[str, dict[str, Any]]:
    """{symbol: {"cents", "date"}} from the newest bar dated before `session_date`
    (every bar when it is None). Symbols without such a bar are absent."""
    if not symbols:
        return {}
    rows = conn.execute(
        """
        SELECT DISTINCT ON (symbol) symbol, close, (ts AT TIME ZONE 'America/New_York')::date AS day
          FROM stock_bars
         WHERE timeframe = '1Day' AND symbol = ANY(%s)
           AND (%s::date IS NULL OR (ts AT TIME ZONE 'America/New_York')::date < %s::date)
         ORDER BY symbol, ts DESC
        """,
        (list(symbols), session_date, session_date),
    ).fetchall()
    return {r["symbol"]: {"cents": cents(r["close"]), "date": r["day"]} for r in rows if r["close"] and r["close"] > 0}


def previous_weekday(day: date) -> date:
    out = day - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def bars_reach_previous_session(
    conn: psycopg.Connection, symbols: list[str], prices: dict[str, dict[str, Any]], session_date: date,
) -> tuple[bool, date | None]:
    """(ok, bars_through): every symbol has a reference price, all of the same day, and
    that day is the previous session. Without a trading calendar the previous session is
    the previous weekday, or an earlier day when every symbol's feed ran after that
    weekday's close and found no bar there (a holiday). A stalled feed is never ok."""
    if not symbols or any(s not in prices for s in symbols):
        return False, None
    days = {prices[s]["date"] for s in symbols}
    through = min(days)
    if len(days) != 1:
        return False, through
    expected = previous_weekday(session_date)
    if through == expected:
        return True, through
    if through > expected:
        return False, through
    after = datetime.combine(expected, FEED_AFTER_CLOSE, tzinfo=NEW_YORK)
    row = conn.execute(
        "SELECT bool_and(fetched_at IS NOT NULL AND fetched_at >= %s) AS ok FROM instruments WHERE symbol = ANY(%s)",
        (after, list(symbols)),
    ).fetchone()
    holiday_gap = (session_date - through).days <= 5
    return bool(row and row["ok"]) and holiday_gap, through


def tradable_symbols(conn: psycopg.Connection, symbols: list[str]) -> set[str]:
    """The symbols with a tradable instrument and at least one daily bar."""
    rows = conn.execute(
        """
        SELECT i.symbol FROM instruments i
         WHERE i.symbol = ANY(%s) AND i.tradable IS TRUE
           AND EXISTS (SELECT 1 FROM stock_bars b WHERE b.symbol = i.symbol AND b.timeframe = '1Day')
        """,
        (list(symbols),),
    ).fetchall()
    return {r["symbol"] for r in rows}


def server_now(conn: psycopg.Connection) -> datetime:
    """The database clock (every stored timestamp is compared with it)."""
    return conn.execute("SELECT now() AS now").fetchone()["now"]
