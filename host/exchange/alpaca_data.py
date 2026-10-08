"""Alpaca market data for stocks (docs/ALPACA.md "Step 9"): daily bars from
`data.alpaca.markets` and one asset's details from the trading host. GETs only, with the
key headers of host.exchange.alpaca_credentials; every error text is redacted.

`splits` reads the forward and reverse splits of some symbols (GET /v1/corporate-actions,
host/exchange/stock_splits.py applies them to the books).

`daily_bars` asks for many symbols per request and follows `next_page_token`. When a
batch is refused (one bad symbol makes Alpaca refuse the whole request) it asks again
one symbol at a time, so a typo in the settings costs that symbol only. Requests are
spaced `min_gap_s` apart (the free plan allows 200 a minute).
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urlencode

from host.exchange.adapters.base import SourceError, truncate
from host.exchange.adapters.live_http import Http, urllib_http
from host.exchange.alpaca_credentials import DATA_URL, AlpacaCredentials

TIMEOUT_S = 20.0
PAGE_LIMIT = 10_000
MAX_PAGES = 500
BATCH = 50
MIN_GAP_S = 0.4
SPLIT_KINDS = ("forward_splits", "reverse_splits")


class AlpacaDataError(SourceError):
    """Alpaca answered something other than 200, or a body that is not the expected JSON."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlpacaData:
    def __init__(self, creds: AlpacaCredentials, http: Http | None = None, sleep: Callable[[float], None] = time.sleep,
                 min_gap_s: float = MIN_GAP_S) -> None:
        self.creds = creds
        self.http = http or urllib_http
        self.sleep = sleep
        self.min_gap_s = min_gap_s
        self._last = 0.0
        self.requests = 0

    def _get(self, url: str) -> Any:
        wait = self._last + self.min_gap_s - time.monotonic()
        if wait > 0 and self.requests:
            self.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1
        status, _, text = self.http("GET", url, self.creds.headers(), None, TIMEOUT_S)
        text = self.creds.redact(text) or ""
        if status != 200:
            raise AlpacaDataError(f"GET {url.split('?')[0]} answered {status}: {truncate(text, 300)}", status)
        try:
            return json.loads(text)
        except ValueError:
            raise AlpacaDataError(f"GET {url.split('?')[0]} answered a body that is not JSON", status) from None

    def asset(self, symbol: str) -> dict[str, Any]:
        data = self._get(f"{self.creds.base_url}/v2/assets/{quote(symbol, safe='.')}")
        if not isinstance(data, dict):
            raise AlpacaDataError(f"asset {symbol}: unexpected answer")
        return data

    def _bars_request(self, symbols: list[str], start: datetime, end: datetime, feed: str) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        token: str | None = None
        for _ in range(MAX_PAGES):
            query = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": _iso(start), "end": _iso(end),
                     "limit": PAGE_LIMIT, "adjustment": "all", "feed": feed, "sort": "asc"}
            if token:
                query["page_token"] = token
            data = self._get(f"{DATA_URL}/v2/stocks/bars?{urlencode(query)}")
            bars = data.get("bars") if isinstance(data, dict) else None
            if bars is not None and not isinstance(bars, dict):
                raise AlpacaDataError("bars: unexpected answer (bars is not an object)")
            for symbol, rows in (bars or {}).items():
                if isinstance(rows, list):
                    out.setdefault(symbol, []).extend(r for r in rows if isinstance(r, dict))
            token = data.get("next_page_token") if isinstance(data, dict) else None
            if not token:
                return out
        raise AlpacaDataError(f"bars: more than {MAX_PAGES} pages, stopped")

    def daily_bars(self, symbols: list[str], start: datetime, end: datetime, feed: str
                   ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
        """({symbol: raw bars oldest first}, {symbol: error}) for every symbol asked."""
        bars: dict[str, list[dict[str, Any]]] = {}
        errors: dict[str, str] = {}
        for i in range(0, len(symbols), BATCH):
            batch = symbols[i:i + BATCH]
            try:
                bars.update(self._bars_request(batch, start, end, feed))
                continue
            except AlpacaDataError as exc:
                if len(batch) == 1 or exc.status not in (400, 404, 422):
                    for symbol in batch:
                        errors[symbol] = str(exc)
                    continue
            for symbol in batch:
                try:
                    bars.update(self._bars_request([symbol], start, end, feed))
                except SourceError as exc:
                    errors[symbol] = str(exc)
        return bars, errors

    def splits(self, symbols: list[str], start: date, end: date) -> list[dict[str, Any]]:
        """The forward and reverse splits of `symbols` between `start` and `end` (raw
        items: symbol, old_rate, new_rate, ex_date, ...), every page."""
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            query = {"symbols": ",".join(symbols), "types": "forward_split,reverse_split", "start": start.isoformat(),
                     "end": end.isoformat(), "limit": 1000}
            if token:
                query["page_token"] = token
            data = self._get(f"{DATA_URL}/v1/corporate-actions?{urlencode(query)}")
            actions = data.get("corporate_actions") if isinstance(data, dict) else None
            if not isinstance(actions, dict):
                raise AlpacaDataError("corporate actions: unexpected answer")
            for kind in SPLIT_KINDS:
                out.extend(item for item in actions.get(kind) or [] if isinstance(item, dict))
            token = data.get("next_page_token")
            if not token:
                return out
        raise AlpacaDataError(f"corporate actions: more than {MAX_PAGES} pages, stopped")
