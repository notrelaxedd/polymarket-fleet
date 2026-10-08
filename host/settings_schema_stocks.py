"""Validators of the stock settings (docs/ALPACA.md "Step 9"), merged into
host.settings_schema.SCHEMA.

- stocks_enabled: the daily bar feed runs (it also needs the Alpaca keys in exchange.env);
- stock_symbols: 1 to 200 distinct ticker symbols, upper case (BRK.B style allowed);
- stock_history_start: the first day of history, YYYY-MM-DD, 2016-01-01 or later;
- stock_history_feed: "sip" (all US exchanges; free when the request ends more than 15
  minutes ago, which a daily refresh after the close always does) or "iex" (one exchange);
- stock_bars_hour: the local hour (settings `tz`) after which the day's refresh runs, 16..23.

Trading (steps 9.2 to 9.5, migration 0013): the decision lead before the close, tick and
poll intervals, the backtest cost, the price band of a reservation, the per-order,
per-position and daily-loss limits, the default bankroll, the number of assignments, the
search and validation years, and the backtest and paper thresholds of a stock lineage.
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


MAX_CENTS = 10**12


def _int_range(low: int, high: int) -> Validator:
    def check(value: Any) -> str | None:
        ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
        return None if ok else f"must be an integer between {low} and {high}"
    return check


def _number_range(low: float, high: float) -> Validator:
    def check(value: Any) -> str | None:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and low <= value <= high
        return None if ok else f"must be a number between {low} and {high}"
    return check


def _cents_by_mode(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != {"paper", "live"}:
        return "must be an object with integer cents for paper and live"
    for mode in ("paper", "live"):
        if _int_range(0, MAX_CENTS)(value[mode]):
            return f"{mode} must be whole cents, 0 or more"
    return None


def _years(value: Any) -> str | None:
    """[first, last] with last null meaning the last complete year."""
    if not isinstance(value, list) or len(value) != 2:
        return "must be [first year, last year or null]"
    first, last = value
    if _int_range(2016, 2100)(first) or (last is not None and _int_range(2016, 2100)(last)):
        return "years must be between 2016 and 2100"
    if last is not None and last < first:
        return "the last year must not be before the first"
    return None


def _object(fields: dict[str, Validator]) -> Validator:
    def check(value: Any) -> str | None:
        if not isinstance(value, dict) or set(value) != set(fields):
            return "must be an object with " + ", ".join(sorted(fields))
        for name, validator in fields.items():
            problem = validator(value[name])
            if problem:
                return f"{name} {problem}"
        return None
    return check


STOCKS_SCHEMA: dict[str, Validator] = {
    "stocks_enabled": _bool,
    "stock_symbols": _symbols,
    "stock_history_start": _history_start,
    "stock_history_feed": _feed,
    "stock_bars_hour": _hour,
    "stock_decision_lead_min": _int_range(12, 120),
    "stock_trade_tick_s": _int_range(5, 300),
    "stock_cost_bps": _number_range(0, 100),
    "stock_price_band": _number_range(0.005, 0.25),
    "stock_max_order_cents": _int_range(0, MAX_CENTS),
    "stock_max_position_cents": _int_range(0, MAX_CENTS),
    "stock_default_bankroll_cents": _int_range(100, MAX_CENTS),
    "stock_max_daily_loss_cents": _cents_by_mode,
    "stock_max_assignments": _int_range(0, 50),
    "stock_backtest_years": _years,
    "stock_validation_years": _years,
    "thresholds_stock_backtest": _object({"min_sharpe": _number_range(-5, 10), "max_drawdown": _number_range(0, 1),
                                          "min_trades": _int_range(0, 1_000_000), "min_validation_sharpe": _number_range(-5, 10)}),
    "thresholds_stock_paper": _object({"min_days": _int_range(0, 3650), "min_return": _number_range(-1, 10),
                                       "max_drawdown": _number_range(0, 1)}),
    "stock_broker_poll_s": _int_range(5, 3600),
    "stock_orders_poll_s": _int_range(1, 300),
}
