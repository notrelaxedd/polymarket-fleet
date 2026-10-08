"""The daily stock bar feed of the exchange process (docs/ALPACA.md "Step 9.1").

Which symbols: the `stock_symbols` setting. When: a symbol is fetched when it has never
been fetched, and again once a day after `stock_bars_hour` (settings `tz`), the whole
history from `stock_history_start` each time, so the split- and dividend-adjusted
series stays consistent when a split or a dividend changes old bars. A symbol whose
last attempt failed is tried again after RETRY_S. The request ends 20 minutes before
now, which keeps the free plan's delayed SIP history usable. Without the Alpaca keys
the task does nothing (and says so); a symbol that is no longer in the setting keeps
its bars.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from host.exchange import alpaca_credentials
from host.exchange.adapters.base import SourceError
from host.exchange.alpaca_data import AlpacaData, AlpacaDataError
from host.settings import get_settings

log = logging.getLogger(__name__)

RETRY_S = 1800
END_LAG = timedelta(minutes=20)
DEFAULTS: dict[str, Any] = {"stocks_enabled": True, "stock_symbols": ["SPY"], "stock_history_start": "2016-01-01",
                            "stock_history_feed": "sip", "stock_bars_hour": 18, "tz": "America/New_York"}


def stock_settings(conn: Any) -> dict[str, Any]:
    values = get_settings(conn)
    return {key: values.get(key, default) for key, default in DEFAULTS.items()}


def last_cutoff(now: datetime, tz: str, hour: int) -> datetime:
    """The most recent daily refresh time at or before `now` (today's, or yesterday's
    before the hour)."""
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - a bad tz setting falls back to New York
        zone = ZoneInfo("America/New_York")
    local = now.astimezone(zone)
    cutoff = datetime.combine(local.date(), dtime(hour=hour), tzinfo=zone)
    return cutoff if local >= cutoff else cutoff - timedelta(days=1)


def stale_symbols(conn: Any, symbols: list[str], now: datetime, cutoff: datetime) -> list[str]:
    """The symbols to fetch now: never fetched, or last fetched before `cutoff`; a
    symbol whose last attempt failed waits RETRY_S."""
    rows = conn.execute("SELECT symbol, fetched_at, last_error, updated_at FROM instruments WHERE symbol = ANY(%s)",
                        (symbols,)).fetchall()
    known = {r["symbol"]: r for r in rows}
    out = []
    for symbol in symbols:
        row = known.get(symbol)
        if row is None:
            out.append(symbol)
            continue
        if row["last_error"] and row["updated_at"] > now - timedelta(seconds=RETRY_S):
            continue
        if row["fetched_at"] is None or row["fetched_at"] < cutoff:
            out.append(symbol)
    return out


def parse_bar(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One Alpaca bar ({t, o, h, l, c, v, n, vw}) as a row, or None when malformed."""
    try:
        ts = datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00"))
        values = [float(raw[k]) for k in ("o", "h", "l", "c", "v")]
    except (KeyError, TypeError, ValueError):
        return None
    if ts.tzinfo is None or any(v != v or v < 0 for v in values) or min(values[:4]) <= 0:
        return None
    count = raw.get("n")
    vwap = raw.get("vw")
    return {"ts": ts, "open": values[0], "high": values[1], "low": values[2], "close": values[3], "volume": values[4],
            "trade_count": int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else None,
            "vwap": float(vwap) if isinstance(vwap, (int, float)) and not isinstance(vwap, bool) else None}


def _ensure_rows(conn: Any, symbols: list[str]) -> None:
    conn.cursor().executemany("INSERT INTO instruments (symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING",
                              [(s,) for s in symbols])


def _fail(conn: Any, symbol: str, error: str, now: datetime) -> None:
    """The error and the attempt time (the loop's clock, which the retry wait compares with)."""
    conn.execute("UPDATE instruments SET last_error = %s, updated_at = %s WHERE symbol = %s", (error[:500], now, symbol))


def _store_asset(conn: Any, symbol: str, asset: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE instruments SET asset_class = COALESCE(%s, asset_class), name = %s, exchange = %s, status = %s,"
        " tradable = %s, fractionable = %s, shortable = %s, updated_at = now() WHERE symbol = %s",
        (asset.get("class"), asset.get("name"), asset.get("exchange"), asset.get("status"),
         bool(asset.get("tradable")), bool(asset.get("fractionable")), bool(asset.get("shortable")), symbol))


def _store_bars(conn: Any, symbol: str, rows: list[dict[str, Any]], feed: str, now: datetime) -> int:
    conn.cursor().executemany(
        "INSERT INTO stock_bars (symbol, timeframe, ts, open, high, low, close, volume, trade_count, vwap, feed)"
        " VALUES (%s, '1Day', %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (symbol, timeframe, ts) DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high,"
        " low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume, trade_count = EXCLUDED.trade_count,"
        " vwap = EXCLUDED.vwap, feed = EXCLUDED.feed",
        [(symbol, r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["trade_count"], r["vwap"], feed)
         for r in rows])
    conn.execute(
        "UPDATE instruments SET bars_through = (SELECT max(ts) FROM stock_bars WHERE symbol = %s AND timeframe = '1Day'),"
        " bars_count = (SELECT count(*) FROM stock_bars WHERE symbol = %s AND timeframe = '1Day'),"
        " fetched_at = %s, last_error = NULL, updated_at = now() WHERE symbol = %s",
        (symbol, symbol, now, symbol))
    return len(rows)


def refresh(conn: Any, client: AlpacaData, symbols: list[str], start_day: str, feed: str, now: datetime) -> dict[str, Any]:
    """Fetch the assets and the whole daily history of `symbols` and store them.
    {"symbols", "bars", "errors": {symbol: text}, "error": a summary when any failed}."""
    _ensure_rows(conn, symbols)
    errors: dict[str, str] = {}
    valid: list[str] = []
    for symbol in symbols:
        try:
            _store_asset(conn, symbol, client.asset(symbol))
            valid.append(symbol)
        except AlpacaDataError as exc:
            errors[symbol] = "unknown symbol at Alpaca" if exc.status == 404 else str(exc)
        except SourceError as exc:
            errors[symbol] = str(exc)
    start = datetime.combine(date.fromisoformat(start_day), dtime(), tzinfo=timezone.utc)
    raw, bar_errors = client.daily_bars(valid, start, now - END_LAG, feed) if valid else ({}, {})
    errors.update(bar_errors)
    total = 0
    for symbol in valid:
        if symbol in errors:
            continue
        rows = [r for r in (parse_bar(b) for b in raw.get(symbol, [])) if r is not None]
        if not rows:
            errors[symbol] = f"no daily bars returned (feed {feed}, from {start_day})"
            continue
        total += _store_bars(conn, symbol, rows, feed, now)
    for symbol, error in errors.items():
        _fail(conn, symbol, error, now)
    result: dict[str, Any] = {"symbols": symbols, "bars": total, "errors": errors}
    if errors:
        result["error"] = f"{len(errors)} of {len(symbols)} symbol(s) failed: " + "; ".join(
            f"{s}: {e}" for s, e in sorted(errors.items()))[:400]
    return result


class StockFeed:
    """Holds the Alpaca data client between ticks of the exchange loop."""

    def __init__(self, client_factory: Callable[[], AlpacaData | None] | None = None) -> None:
        self.client_factory = client_factory or self.default_client
        self.client: AlpacaData | None = None
        self.config_error: str | None = None
        self.loaded = False

    @staticmethod
    def default_client() -> AlpacaData | None:
        creds = alpaca_credentials.load()
        return AlpacaData(creds) if creds is not None else None

    def get_client(self) -> AlpacaData | None:
        if not self.loaded:
            self.loaded = True
            try:
                self.client = self.client_factory()
            except alpaca_credentials.AlpacaConfigError as exc:
                self.config_error = str(exc)
        return self.client

    def run(self, conn: Any, now: datetime, force: bool = False, only: list[str] | None = None) -> dict[str, Any]:
        """One pass: the stale symbols (or `only`, or all with `force`) are fetched."""
        settings = stock_settings(conn)
        if not settings["stocks_enabled"] and not force:
            return {"skipped": "stocks_enabled is off"}
        client = self.get_client()
        if self.config_error:
            return {"error": self.config_error}
        if client is None:
            return {"skipped": "no Alpaca keys in exchange.env"}
        symbols = list(only or settings["stock_symbols"])
        if not force and not only:
            symbols = stale_symbols(conn, symbols, now, last_cutoff(now, settings["tz"], int(settings["stock_bars_hour"])))
        if not symbols:
            return {"symbols": [], "bars": 0}
        log.info("stock bars: fetching %d symbol(s)", len(symbols))
        return refresh(conn, client, symbols, str(settings["stock_history_start"]), str(settings["stock_history_feed"]), now)


def status(conn: Any) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT symbol, name, tradable, fractionable, bars_count, bars_through, fetched_at, last_error"
        " FROM instruments ORDER BY symbol").fetchall()
