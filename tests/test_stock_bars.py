"""The daily stock bar feed (docs/ALPACA.md "Step 9.1"): settings validation, when a
symbol is fetched, pagination and the one-symbol fallback, adjusted history replaced
in place, failures recorded per symbol, and nothing at all without the Alpaca keys."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from host import settings
from host.errors import BadRequest
from host.exchange import alpaca_credentials as ac
from host.exchange import cli, stock_bars
from host.exchange.alpaca_data import AlpacaData
from host.exchange.main import ExchangeLoop, run_once

KEY, SECRET = "PKTESTKEY000000000000WXYZ", "secret-never-printed-0123456789"
NOW = datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc)  # 19:30 in New York, after stock_bars_hour


def bar(day: str, close: float) -> dict[str, Any]:
    return {"t": f"{day}T04:00:00Z", "o": close, "h": close + 1, "l": close - 1, "c": close, "v": 1000, "n": 10, "vw": close}


class FakeData:
    """Alpaca's asset and bars endpoints; `bars` maps symbol to its bars, `pages` splits answers."""

    def __init__(self, bars: dict[str, list[dict[str, Any]]], unknown: tuple[str, ...] = (), page_size: int = 2) -> None:
        self.bars, self.unknown, self.page_size = bars, unknown, page_size
        self.urls: list[str] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: Any, timeout: float):
        assert method == "GET" and headers["APCA-API-KEY-ID"] == KEY
        self.urls.append(url)
        parts = urlsplit(url)
        if "/v2/assets/" in parts.path:
            symbol = parts.path.rsplit("/", 1)[1]
            if symbol in self.unknown:
                return 404, {}, '{"message": "asset not found"}'
            return 200, {}, json.dumps({"symbol": symbol, "name": f"{symbol} Inc", "class": "us_equity", "tradable": True,
                                         "fractionable": True, "shortable": False, "status": "active"})
        query = parse_qs(parts.query)
        symbols = query["symbols"][0].split(",")
        if any(s in self.unknown for s in symbols):
            return 400, {}, f'{{"message": "invalid symbol {KEY}"}}'
        assert query["adjustment"] == ["all"] and query["timeframe"] == ["1Day"]
        rows = [(s, b) for s in symbols for b in self.bars.get(s, [])]
        offset = int(query.get("page_token", ["0"])[0])
        page = rows[offset:offset + self.page_size]
        out: dict[str, list[dict[str, Any]]] = {}
        for symbol, b in page:
            out.setdefault(symbol, []).append(b)
        token = str(offset + self.page_size) if offset + self.page_size < len(rows) else None
        return 200, {}, json.dumps({"bars": out, "next_page_token": token})


def feed_for(fake: FakeData) -> stock_bars.StockFeed:
    creds = ac.load({ac.KEY_VAR: KEY, ac.SECRET_VAR: SECRET})
    return stock_bars.StockFeed(lambda: AlpacaData(creds, http=fake, sleep=lambda s: None))


def bars_of(conn, symbol: str) -> list[tuple[str, float]]:
    rows = conn.execute("SELECT ts, close FROM stock_bars WHERE symbol = %s ORDER BY ts", (symbol,)).fetchall()
    return [(r["ts"].date().isoformat(), r["close"]) for r in rows]


def test_seeded_settings_validate_and_bad_values_are_refused(conn) -> None:
    values = settings.get_settings(conn)
    assert values["stocks_enabled"] is True and "SPY" in values["stock_symbols"] and values["stock_history_feed"] == "sip"
    for key, bad in (("stock_symbols", ["spy"]), ("stock_symbols", ["SPY", "SPY"]), ("stock_symbols", []),
                     ("stock_history_start", "2015-12-31"), ("stock_history_feed", "polygon"), ("stock_bars_hour", 9)):
        with pytest.raises(BadRequest):
            settings.set_settings(conn, {key: bad}, "test")
    settings.set_settings(conn, {"stock_symbols": ["SPY", "BRK.B"], "stock_bars_hour": 17}, "test")


def test_first_pass_fetches_everything_with_pagination_and_stores_assets(conn) -> None:
    settings.set_settings(conn, {"stock_symbols": ["SPY", "AAPL"]}, "test")
    fake = FakeData({"SPY": [bar("2026-10-06", 500), bar("2026-10-07", 501)], "AAPL": [bar("2026-10-07", 200)]})
    result = feed_for(fake).run(conn, NOW)
    assert result["bars"] == 3 and result["errors"] == {} and "error" not in result
    assert sum("page_token" in u for u in fake.urls) == 1, "the second page is asked for with the token"
    assert bars_of(conn, "SPY") == [("2026-10-06", 500.0), ("2026-10-07", 501.0)]
    row = conn.execute("SELECT * FROM instruments WHERE symbol = 'AAPL'").fetchone()
    assert row["name"] == "AAPL Inc" and row["fractionable"] is True and row["bars_count"] == 1 and row["last_error"] is None
    bars_url = next(u for u in fake.urls if "/v2/stocks/bars" in u)
    end = parse_qs(urlsplit(bars_url).query)["end"][0]
    assert end == (NOW - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), "ends 20 minutes back (delayed SIP)"


def test_daily_schedule_new_symbols_at_once_and_adjusted_history_replaced(conn) -> None:
    settings.set_settings(conn, {"stock_symbols": ["SPY"]}, "test")
    fake = FakeData({"SPY": [bar("2026-10-07", 500)]})
    feed = feed_for(fake)
    morning = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)  # 10:00 New York, before the hour
    assert feed.run(conn, morning)["bars"] == 1, "a never fetched symbol is fetched at once"
    assert feed.run(conn, morning + timedelta(hours=1)) == {"symbols": [], "bars": 0}, "then not again before the hour"
    fake.bars["SPY"] = [bar("2026-10-07", 250), bar("2026-10-08", 252)]  # a 2:1 split adjusts the old bar
    assert feed.run(conn, NOW)["bars"] == 2, "after stock_bars_hour the whole history is fetched again"
    assert bars_of(conn, "SPY") == [("2026-10-07", 250.0), ("2026-10-08", 252.0)]
    assert feed.run(conn, NOW + timedelta(minutes=5)) == {"symbols": [], "bars": 0}, "once a day"


def test_an_unknown_symbol_fails_alone_and_waits_before_a_retry(conn) -> None:
    settings.set_settings(conn, {"stock_symbols": ["SPY", "ZZZZ"]}, "test")
    fake = FakeData({"SPY": [bar("2026-10-07", 500)]}, unknown=("ZZZZ",))
    feed = feed_for(fake)
    result = feed.run(conn, NOW)
    assert result["bars"] == 1 and result["errors"] == {"ZZZZ": "unknown symbol at Alpaca"} and "ZZZZ" in result["error"]
    assert conn.execute("SELECT last_error FROM instruments WHERE symbol = 'ZZZZ'").fetchone()["last_error"]
    assert feed.run(conn, NOW + timedelta(minutes=1)) == {"symbols": [], "bars": 0}, "the failed symbol waits RETRY_S"


def test_a_refused_batch_is_asked_again_one_symbol_at_a_time_and_errors_are_redacted(conn) -> None:
    fake = FakeData({"SPY": [bar("2026-10-07", 500)]}, unknown=("BAD",))
    client = AlpacaData(ac.load({ac.KEY_VAR: KEY, ac.SECRET_VAR: SECRET}), http=fake, sleep=lambda s: None)
    start = datetime(2016, 1, 1, tzinfo=timezone.utc)
    bars, errors = client.daily_bars(["SPY", "BAD"], start, NOW, "sip")
    assert [b["c"] for b in bars["SPY"]] == [500] and set(errors) == {"BAD"}
    assert KEY not in errors["BAD"] and "***WXYZ" in errors["BAD"]


def test_without_keys_or_switched_off_nothing_is_fetched(conn) -> None:
    assert stock_bars.StockFeed(lambda: None).run(conn, NOW) == {"skipped": "no Alpaca keys in exchange.env"}
    settings.set_settings(conn, {"stocks_enabled": False}, "test")
    assert feed_for(FakeData({})).run(conn, NOW) == {"skipped": "stocks_enabled is off"}


def test_the_exchange_loop_runs_the_task_and_the_cli_reports(conn, pool, config, monkeypatch, capsys) -> None:
    for name in (ac.KEY_VAR, ac.SECRET_VAR, ac.BASE_URL_VAR):
        monkeypatch.delenv(name, raising=False)
    results = run_once(pool, NOW, ExchangeLoop(pool, clock=lambda: NOW))
    assert results["stocks"] == {"skipped": "no Alpaca keys in exchange.env"}
    monkeypatch.setenv("DATABASE_URL", config.database_url)
    assert cli.main(["ingest-stock-bars"]) == 1 and "no Alpaca keys" in capsys.readouterr().out
    fake = FakeData({"SPY": [bar("2026-10-07", 500)]})
    monkeypatch.setattr(stock_bars.StockFeed, "default_client",
                        staticmethod(lambda: AlpacaData(ac.load({ac.KEY_VAR: KEY, ac.SECRET_VAR: SECRET}), http=fake, sleep=lambda s: None)))
    assert cli.main(["ingest-stock-bars", "--symbol", "SPY"]) == 0
    assert cli.main(["stock-bars-status"]) == 0 and "SPY" in capsys.readouterr().out
