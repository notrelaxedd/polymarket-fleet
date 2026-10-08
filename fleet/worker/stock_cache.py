"""Worker cache of the daily stock bars (contract section 3), like pbp_cache.

refresh_stock_bars() keeps <state>/cache/stock_bars.json current with a conditional
GET /api/v1/data/stock_bars: the stored ETag (stock_bars.etag) goes out as
If-None-Match, a 304 keeps the file, a 200 body {"generated_at", "symbols": {...}} is
checked and written atomically. A failed fetch falls back to the cached file when
there is one (warning), else raises StockCacheError.

build_context() is the runner context of the batch stock kinds (stock_search,
stock_backtest, stock_validate): {"stock_bars_path": <cache file>}; the job reads it
as params["_context"]["stock_bars_path"]. A failure is a context.ContextError, so the
agent fails the job like any other context failure.

BarsMemo keeps the parsed bars of the cache file in memory for the trade loop and
reloads them only when the file changed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fleet.common import http
from fleet.stocks.data import Bars, load_bars
from fleet.worker import config
from fleet.worker.context import ContextError

log = logging.getLogger("fleet.stock_cache")

STOCK_BARS_PATH = "/api/v1/data/stock_bars"
BARS_FILE = "stock_bars.json"
ETAG_FILE = "stock_bars.etag"
BATCH_KINDS = ("stock_search", "stock_backtest", "stock_validate")
DEFAULT_TIMEOUT = 120.0


class StockCacheError(Exception):
    """The bars are unavailable and there is no cached copy."""


def bars_path(state_dir: str) -> str:
    return os.path.join(config.cache_dir(state_dir), BARS_FILE)


def etag_path(state_dir: str) -> str:
    return os.path.join(config.cache_dir(state_dir), ETAG_FILE)


def needs_context(kind: Any) -> bool:
    return isinstance(kind, str) and kind in BATCH_KINDS


def _read_etag(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    return value or None


def _write_etag(path: str, etag: str | None) -> None:
    if not etag:
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(etag + "\n")
    os.replace(tmp, path)


def refresh_stock_bars(host_url: str, token: str, state_dir: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Refresh the cache with a conditional GET and return the cache file path."""
    path = bars_path(state_dir)
    tag_path = etag_path(state_dir)
    cached = os.path.isfile(path)
    etag = _read_etag(tag_path) if cached else None
    url = host_url + STOCK_BARS_PATH
    try:
        resp = http.get_json_etag(url, token=token, etag=etag, timeout=timeout)
        if resp.status == 304:
            log.debug("stock bars cache unchanged (%s)", etag)
            return path
        body = resp.body
        if not isinstance(body, dict) or not isinstance(body.get("symbols"), dict):
            raise http.HttpConnectionError(f"GET {url}: unexpected body shape ({type(body).__name__})")
        os.makedirs(config.cache_dir(state_dir), exist_ok=True)
        config.write_json_atomic(path, body)
        _write_etag(tag_path, resp.etag)
    except (http.HttpError, http.HttpConnectionError, OSError) as exc:
        if cached:
            log.warning("stock bars refresh failed (%s); using the cached %s", exc, path)
            return path
        raise StockCacheError(f"stock bars unavailable and no cached copy at {path}: {exc}") from None
    log.info("stock bars cache refreshed: %d symbols, etag %s", len(body["symbols"]), resp.etag)
    return path


def build_context(host_url: str, token: str, state_dir: str, job: dict[str, Any],
                  timeout: float, data_timeout: float) -> dict[str, Any]:
    """{"stock_bars_path"} for a batch stock job; ContextError when the bars are unavailable."""
    try:
        return {"stock_bars_path": refresh_stock_bars(host_url, token, state_dir, max(timeout, data_timeout))}
    except StockCacheError as exc:
        raise ContextError(str(exc)) from None


class BarsMemo:
    """The parsed bars of one cache file, reloaded when the file is replaced."""

    def __init__(self) -> None:
        self.path: str | None = None
        self.stamp: tuple[int, int, int] | None = None
        self.bars: Bars = {}

    def load(self, path: str) -> Bars:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        if path != self.path or stamp != self.stamp:
            self.bars = load_bars(path)
            self.path, self.stamp = path, stamp
        return self.bars
