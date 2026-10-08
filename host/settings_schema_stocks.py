"""Validators of the stock settings (docs/ALPACA.md "Step 9"), merged into
host.settings_schema.SCHEMA.

- stocks_enabled: the daily bar feed runs (it also needs the Alpaca keys in exchange.env);
- stock_symbols: 1 to 200 distinct ticker symbols, upper case (BRK.B style allowed);
- stock_history_start: the first day of history, YYYY-MM-DD, 2016-01-01 or later;
- stock_history_feed: "sip" (all US exchanges; free when the request ends more than 15
  minutes ago, which a daily refresh after the close always does) or "iex" (one exchange);
- stock_bars_hour: the local hour (settings `tz`) after which the day's refresh runs, 16..23.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

Validator = Callable[[Any], str | None]
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
MAX_SYMBOLS = 200
FIRST_DAY = date(2016, 1, 1)
FEEDS = ("sip", "iex")


def _bool(value: Any) -> str | None:
    return None if isinstance(value, bool) else "must be true or false"


def _symbols(value: Any) -> str | None:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SYMBOLS:
        return f"must be a list of 1 to {MAX_SYMBOLS} ticker symbols"
    for symbol in value:
        if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
            return f"{symbol!r} is not a ticker symbol (upper case letters, digits and '.', such as SPY or BRK.B)"
    if len(set(value)) != len(value):
        return "must not repeat a symbol"
    return None


def _history_start(value: Any) -> str | None:
    try:
        day = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        day = None
    if day is None:
        return "must be a date such as 2016-01-01"
    if day < FIRST_DAY or day > date.today():
        return f"must be between {FIRST_DAY.isoformat()} and today"
    return None


def _feed(value: Any) -> str | None:
    return None if value in FEEDS else "must be sip or iex"


def _hour(value: Any) -> str | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and 16 <= value <= 23
    return None if ok else "must be an hour between 16 and 23"


STOCKS_SCHEMA: dict[str, Validator] = {
    "stocks_enabled": _bool,
    "stock_symbols": _symbols,
    "stock_history_start": _history_start,
    "stock_history_feed": _feed,
    "stock_bars_hour": _hour,
}
