"""Market source adapters and the factory that picks one from settings."""
from __future__ import annotations

from typing import Any

import psycopg

from host.exchange.adapters.base import Book, MarketInfo, MarketSource, NotConfigured, OrderGateway, PaperGateway, SourceError
from host.settings import get_setting

SOURCES = ("sim", "polymarket_us", "polymarket_clob")


def make_source(name: str, config: dict[str, Any] | None = None) -> MarketSource:
    """A MarketSource by settings name; the per-source block of market_source_config."""
    config = config or {}
    if name == "sim":
        from host.exchange.adapters.sim import SimSource

        return SimSource()
    if name == "polymarket_us":
        from host.exchange.adapters.polymarket_us import PolymarketUSSource

        return PolymarketUSSource(config.get("polymarket_us"))
    if name == "polymarket_clob":
        from host.exchange.adapters.polymarket_clob import PolymarketClobSource

        return PolymarketClobSource(config.get("polymarket_clob"))
    raise SourceError(f"unknown market source {name!r}")


def source_from_settings(conn: psycopg.Connection) -> MarketSource:
    name = get_setting(conn, "market_source", "sim")
    config = get_setting(conn, "market_source_config", {}) or {}
    return make_source(str(name), config if isinstance(config, dict) else {})


__all__ = [
    "Book", "MarketInfo", "MarketSource", "NotConfigured", "OrderGateway", "PaperGateway", "SourceError",
    "SOURCES", "make_source", "source_from_settings",
]
