"""The market source and order gateway interfaces (docs/TRADING.md, "Market data sources").

Prices are floats in (0, 1) per $1 contract, sizes are whole contracts, levels are
`[price, size]` lists sorted best first (highest bid, lowest ask).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

Level = list[float]


class NotConfigured(Exception):
    """A live gateway call made before step 5 wires the exchange keys."""


class SourceError(Exception):
    """A market source could not answer (network, bad payload)."""


class RateLimited(SourceError):
    """The source answered 429: the caller halves its request rate for a minute."""


def check_status(status: int, text: str | None, what: str) -> None:
    """Raise for a non-200 answer: RateLimited on 429, SourceError otherwise."""
    if status == 200:
        return
    if status == 429:
        raise RateLimited(f"{what} request answered 429 (rate limited): {truncate(text, 256)}")
    raise SourceError(f"{what} request answered {status}: {truncate(text, 512)}")


MIN_SIZE_CAP = 1_000_000


def clamp_min_size(value: Any, default: int = 1) -> int:
    """A market's minimum order size as a whole number in 1..1_000_000; anything
    missing, non-numeric, non-finite or absurd falls back to `default`."""
    number = _number(value)
    if number is None or number < 1 or number > MIN_SIZE_CAP:
        return default
    return int(number)


@dataclass
class MarketInfo:
    """One tradable YES contract. `side` says which of home/away the YES pays."""

    platform: str
    market_ref: str
    event_ref: str | None
    title: str
    home_team: str | None
    away_team: str | None
    side: str | None
    kickoff_at: datetime | None
    tick: float = 0.01
    min_size: int = 1
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Book:
    """Bids and asks, up to 10 levels each, best first."""

    bids: list[Level]
    asks: list[Level]
    fetched_at: datetime

    @property
    def best_bid(self) -> float | None:
        return float(self.bids[0][0]) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return float(self.asks[0][0]) if self.asks else None

    @property
    def mid(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return bid if bid is not None else ask


def clean_levels(levels: Any, descending: bool, limit: int = 10) -> list[Level]:
    """Keep well-formed `[price, size]` (or `{price, size}`) levels with a price in
    (0, 1) and a positive size, sorted best first, at most `limit` of them."""
    out: list[Level] = []
    if not isinstance(levels, (list, tuple)):
        return out
    for level in levels:
        price, size = _level_fields(level)
        if price is None or size is None or not 0.0 < price < 1.0 or size <= 0:
            continue
        out.append([price, size])
    out.sort(key=lambda lv: lv[0], reverse=descending)
    return out[:limit]


def _level_fields(level: Any) -> tuple[float | None, float | None]:
    if isinstance(level, dict):
        return _number(level.get("price")), _number(level.get("size"))
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return _number(level[0]), _number(level[1])
    return None, None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketSource:
    """Read-only market data: which YES contracts exist and their books. A book
    fetch gets a short timeout (`book_timeout`) so one slow source cannot hold
    the exchange loop for long."""

    name: str = "base"
    book_timeout: float = 2.0

    def list_markets(self, games: list[dict[str, Any]], lookahead_days: int) -> list[MarketInfo]:
        raise NotImplementedError

    def fetch_book(self, market_ref: str) -> Book:
        raise NotImplementedError

    def probe(self) -> dict[str, Any]:
        """The raw markets payload (text) and the URL it came from, for the owner."""
        return {"url": None, "status": None, "payload": None}


class OrderGateway:
    """Order placement on the exchange. Live (step 5) is not configured in step 4."""

    name: str = "none"

    def place(self, order: dict[str, Any]) -> str:
        """Submit an order; returns the exchange order id."""
        raise NotConfigured("live order placement is not configured (step 5)")

    def cancel(self, order: dict[str, Any]) -> bool:
        raise NotConfigured("live cancel is not configured (step 5)")

    def open_orders(self) -> list[dict[str, Any]]:
        raise NotConfigured("live open_orders is not configured (step 5)")

    def fills(self, since: datetime | None) -> list[dict[str, Any]]:
        raise NotConfigured("live fills is not configured (step 5)")

    def balance(self) -> dict[str, Any]:
        raise NotConfigured("live balance is not configured (step 5)")


class PaperGateway(OrderGateway):
    """Paper orders are open the moment they are placed; fills come from paper.py."""

    name = "paper"

    def place(self, order: dict[str, Any]) -> str:
        return f"paper:{order['client_request_id']}"

    def cancel(self, order: dict[str, Any]) -> bool:
        return True

    def open_orders(self) -> list[dict[str, Any]]:
        return []

    def fills(self, since: datetime | None) -> list[dict[str, Any]]:
        return []

    def balance(self) -> dict[str, Any]:
        return {"balance_cents": None, "buying_power_cents": None}


def parse_time(value: Any) -> datetime | None:
    """An ISO 8601 string (Z or offset) or epoch seconds to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def http_get(url: str, timeout: float = 10.0, max_bytes: int = 8 * 1024 * 1024) -> tuple[int, str]:
    """GET a URL; (status, body text). Raises SourceError on any network failure."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-fleet/0.4", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read(max_bytes) if exc.fp else b""
        return int(exc.code), body.decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SourceError(f"GET {url} failed: {exc}") from exc
    if len(body) > max_bytes:
        raise SourceError(f"GET {url}: body over {max_bytes} bytes")
    return status, body.decode("utf-8", "replace")


def truncate(text: str | None, limit: int = 64 * 1024) -> str | None:
    """At most `limit` characters of a payload (for logs and the probe endpoint)."""
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + f"... [truncated, {len(text)} chars]"
