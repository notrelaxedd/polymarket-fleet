"""Daily bars for the stock models (contract section 3).

The host's GET /api/v1/data/stock_bars body is {"generated_at", "symbols": {"SPY":
[["2016-01-04", o, h, l, c, v], ...]}}: adjusted 1Day bars, dates in New York, oldest
first. parse_bars() turns it into {symbol: [Bar, ...]} (sorted by date, duplicates and
malformed rows dropped). history_before() is the one place a model's view is cut: it
keeps only the bars dated strictly before the decision day, so the backtest and the
live trade tick give a model exactly the same history for the same day.
"""

from __future__ import annotations

import bisect
import json
import math
from typing import Any, Iterable, NamedTuple

BENCHMARK = "SPY"


class Bar(NamedTuple):
    """One adjusted daily bar; date is YYYY-MM-DD (New York)."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


Bars = dict[str, list[Bar]]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def parse_row(row: Any) -> Bar | None:
    """A Bar from [date, o, h, l, c, v]; None for anything malformed or a close <= 0."""
    if not isinstance(row, (list, tuple)) or len(row) < 6 or not isinstance(row[0], str) or len(row[0]) != 10:
        return None
    nums = [_finite(v) for v in row[1:6]]
    if any(v is None for v in nums) or nums[3] is None or nums[3] <= 0:
        return None
    o, h, low, c, v = (float(x) for x in nums)  # type: ignore[arg-type]
    return Bar(row[0], o, h, low, c, v)


def parse_bars(body: Any) -> Bars:
    """{symbol: bars sorted by date} from the feed body (or a bare {symbol: rows} dict)."""
    symbols = body.get("symbols") if isinstance(body, dict) and isinstance(body.get("symbols"), dict) else body
    if not isinstance(symbols, dict):
        raise ValueError("stock bars: expected an object with a symbols map")
    out: Bars = {}
    for symbol, rows in symbols.items():
        if not isinstance(symbol, str) or not isinstance(rows, list):
            continue
        by_date: dict[str, Bar] = {}
        for row in rows:
            bar = parse_row(row)
            if bar is not None:
                by_date[bar.date] = bar
        if by_date:
            out[symbol] = [by_date[d] for d in sorted(by_date)]
    return out


def load_bars(path: str) -> Bars:
    """The bars of a cache file written by fleet.worker.stock_cache."""
    with open(path, "r", encoding="utf-8") as fh:
        return parse_bars(json.load(fh))


def last_date(bars: Bars, symbols: Iterable[str] | None = None) -> str | None:
    """The newest bar date over the given symbols (all when None)."""
    names = list(bars) if symbols is None else [s for s in symbols if s in bars]
    dates = [bars[s][-1].date for s in names if bars.get(s)]
    return max(dates) if dates else None


def dates_of(bars: Bars) -> dict[str, list[str]]:
    """The date column of every symbol (for repeated cuts with bisect)."""
    return {symbol: [b.date for b in rows] for symbol, rows in bars.items()}


def history_before(bars: Bars, day: str, symbols: Iterable[str],
                   dates: dict[str, list[str]] | None = None) -> Bars:
    """{symbol: the bars dated strictly before day} for the listed symbols present in
    bars. This is all a model ever sees for a decision on `day`."""
    out: Bars = {}
    for symbol in symbols:
        rows = bars.get(symbol)
        if not rows:
            continue
        column = dates[symbol] if dates is not None and symbol in dates else [b.date for b in rows]
        cut = bisect.bisect_left(column, day)
        if cut:
            out[symbol] = rows[:cut]
    return out


def close_on(bars: Bars, symbol: str, day: str, dates: dict[str, list[str]] | None = None) -> float | None:
    """The close of symbol on exactly day, None when there is no bar that day."""
    rows = bars.get(symbol)
    if not rows:
        return None
    column = dates[symbol] if dates is not None and symbol in dates else [b.date for b in rows]
    i = bisect.bisect_left(column, day)
    return rows[i].close if i < len(rows) and rows[i].date == day else None


def trading_days(bars: Bars, symbols: Iterable[str], first_year: int, last_year: int) -> list[str]:
    """The session dates of the years first..last: SPY's dates when SPY is in the data,
    else the union of the symbols' dates."""
    lo, hi = f"{int(first_year):04d}-01-01", f"{int(last_year):04d}-12-31"
    if bars.get(BENCHMARK):
        days = {b.date for b in bars[BENCHMARK]}
    else:
        days = {b.date for s in symbols for b in bars.get(s, [])}
    return sorted(d for d in days if lo <= d <= hi)


def data_years(bars: Bars) -> tuple[int, int] | None:
    """(first, last) calendar year present in the data."""
    dates = [rows[0].date for rows in bars.values() if rows] + [rows[-1].date for rows in bars.values() if rows]
    if not dates:
        return None
    return int(min(dates)[:4]), int(max(dates)[:4])
