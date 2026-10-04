"""Polymarket US gateway, public reads only (docs/TRADING.md). The endpoint paths and
field names are UNVERIFIED: every one of them comes from
`settings.market_source_config.polymarket_us` with the defaults below, parsing is
defensive (every field optional) and an unknown shape is logged at DEBUG with the raw
payload truncated. `probe()` returns the raw markets payload for the owner.

Assumed shape (defaults): `GET {base_url}{markets_path}?{sport_query}` answers a JSON
list (or `{"markets": [...]}` / `{"data": [...]}`) of markets with `id`, `event_id`,
`title`, `home_team`, `away_team`, `outcome` (the team the YES pays), `start_time`,
`tick_size`, `min_order_size`; `GET {base_url}{book_path}` (with `{market_ref}`
substituted) answers `{"bids": [{"price", "size"}], "asks": [...]}`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from host.exchange.adapters import teams
from host.exchange.adapters.base import (
    Book, MarketInfo, MarketSource, SourceError, check_status, clamp_min_size, clean_levels, http_get, parse_time,
    truncate, utcnow,
)

log = logging.getLogger(__name__)

PLATFORM = "polymarket_us"
DEFAULTS: dict[str, Any] = {
    "base_url": "https://gateway.polymarket.us",
    "markets_path": "/v1/markets",
    "book_path": "/v1/markets/{market_ref}/book",
    "sport_query": "sport=nfl",
    "fields": {
        "id": ["id", "market_id", "marketId", "slug"],
        "event_id": ["event_id", "eventId", "event"],
        "title": ["title", "question", "name"],
        "home_team": ["home_team", "homeTeam", "home"],
        "away_team": ["away_team", "awayTeam", "away"],
        "outcome": ["outcome", "team", "side", "yes_team"],
        "start_time": ["start_time", "startTime", "game_start_time", "gameStartTime", "start_date", "startDate"],
        "tick": ["tick_size", "tickSize", "tick"],
        "min_size": ["min_order_size", "minOrderSize", "min_size"],
        "status": ["status", "state"],
    },
}


def config_with_defaults(config: dict[str, Any] | None) -> dict[str, Any]:
    out = json.loads(json.dumps(DEFAULTS))
    for key, value in (config or {}).items():
        if key == "fields" and isinstance(value, dict):
            for name, names in value.items():
                out["fields"][name] = [names] if isinstance(names, str) else list(names)
        else:
            out[key] = value
    return out


# Step 5 (docs/LIVE.md): the authenticated endpoints. UNVERIFIED like the public ones:
# every path, method, request field name and response field candidate list is
# configurable under `market_source_config.polymarket_us.live`; the signing rules under
# `.auth` (defaults in host/exchange/adapters/signing.py). An endpoint is
# {"method", "path"} or the string "METHOD /path"; `cancel_all` null means
# list-open-then-cancel-each. Money comes back in `money_unit` ("dollars" or "cents").
LIVE_DEFAULTS: dict[str, Any] = {
    "base_url": "https://api.polymarket.us",
    "place": {"method": "POST", "path": "/v1/orders"},
    "cancel": {"method": "DELETE", "path": "/v1/orders/{order_id}"},
    "cancel_all": None,
    "open": {"method": "GET", "path": "/v1/orders/open"},
    "order": {"method": "GET", "path": "/v1/order/{order_id}"},
    "fills": {"method": "GET", "path": "/v1/fills", "since_param": "since", "since_format": "iso"},
    "balance": {"method": "GET", "path": "/v1/balance"},
    "side_buy": "BUY",
    "time_in_force": "GTD",
    "client_id_field": "client_request_id",
    "money_unit": "dollars",
    "timeout_s": 10.0,
    "limiter_wait_s": 2.0,
    "request_fields": {
        "client_order_id": "client_order_id",
        "market_id": "market_id",
        "side": "side",
        "price": "price",
        "size": "size",
        "time_in_force": "time_in_force",
        "expires_at": "expires_at",
    },
    "response_fields": {
        "order_id": ["order_id", "orderId", "id"],
        "client_order_id": ["client_order_id", "clientOrderId", "client_id", "clientId"],
        "market_id": ["market_id", "marketId", "market"],
        "price": ["price", "limit_price", "limitPrice"],
        "size": ["size", "quantity", "original_size", "originalSize"],
        "filled_size": ["filled_size", "filledSize", "filled", "size_matched", "sizeMatched"],
        "status": ["status", "state"],
        "fill_id": ["fill_id", "fillId", "trade_id", "tradeId", "id"],
        "fee": ["fee", "fee_cents", "fees", "commission"],
        "fill_time": ["timestamp", "ts", "time", "filled_at", "filledAt", "created_at", "createdAt", "match_time"],
        "balance": ["balance", "cash", "cash_balance", "cashBalance", "available_balance"],
        "buying_power": ["buying_power", "buyingPower", "available", "available_funds"],
        "server_time": ["server_time", "serverTime", "timestamp", "time"],
        "cancelled_count": ["cancelled", "canceled", "cancelled_count", "count"],
    },
    "list_keys": ["orders", "fills", "data", "results", "items"],
    "wrapper_keys": ["order", "data", "result", "account"],
}


def _merge(defaults: dict[str, Any], overrides: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(defaults))
    if not isinstance(overrides, dict):
        return out
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "request_fields":
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def live_config_with_defaults(config: dict[str, Any] | None) -> dict[str, Any]:
    """The whole polymarket_us block with the public defaults plus the `auth` and
    `live` blocks filled in (a response field given as a string becomes a one-item
    candidate list)."""
    from host.exchange.adapters.signing import AUTH_DEFAULTS

    cfg = dict(config or {})
    out = config_with_defaults({k: v for k, v in cfg.items() if k not in ("auth", "live")})
    out["auth"] = _merge(AUTH_DEFAULTS, cfg.get("auth"))
    live = _merge(LIVE_DEFAULTS, cfg.get("live"))
    for name, names in list(live["response_fields"].items()):
        live["response_fields"][name] = [names] if isinstance(names, str) else list(names)
    out["live"] = live
    return out


def pick(record: dict[str, Any], names: list[str]) -> Any:
    """The first present, non-null field among `names` (dotted paths allowed)."""
    for name in names:
        value: Any = record
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def market_records(payload: Any) -> list[dict[str, Any]]:
    """The list of market objects in a payload of any of the accepted shapes."""
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if isinstance(payload, dict):
        for key in ("markets", "data", "results", "items"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [m for m in inner if isinstance(m, dict)]
    return []


def _team(value: Any) -> str | None:
    if isinstance(value, dict):
        value = pick(value, ["abbreviation", "code", "name", "displayName"])
    return teams.resolve(value) if isinstance(value, str) else None


def parse_market(record: dict[str, Any], fields: dict[str, list[str]]) -> MarketInfo | None:
    """A MarketInfo from one market object; None (logged at DEBUG) when the record has
    no usable id, teams or side. Unresolvable teams leave the market unmatched."""
    ref = pick(record, fields["id"])
    if ref is None:
        log.debug("polymarket_us market without id: %s", truncate(json.dumps(record), 2048))
        return None
    title = pick(record, fields["title"])
    title = str(title) if title is not None else str(ref)
    home = _team(pick(record, fields["home_team"]))
    away = _team(pick(record, fields["away_team"]))
    found = teams.find(title)
    if home is None and away is None and len(found) == 2:
        away, home = found[0], found[1]
    outcome = _team(pick(record, fields["outcome"]))
    if outcome is None and len(found) == 1:
        outcome = found[0]
    side = None
    if outcome is not None:
        side = "home" if outcome == home else "away" if outcome == away else None
    tick = _float(pick(record, fields["tick"]), 0.01)
    min_size = clamp_min_size(pick(record, fields["min_size"]))
    return MarketInfo(
        platform=PLATFORM,
        market_ref=str(ref),
        event_ref=_text(pick(record, fields["event_id"])),
        title=title,
        home_team=home,
        away_team=away,
        side=side,
        kickoff_at=parse_time(pick(record, fields["start_time"])),
        tick=tick if 0 < tick < 1 else 0.01,
        min_size=min_size,
        raw=record,
    )


def _float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def parse_markets(payload_text: str, config: dict[str, Any] | None = None) -> list[MarketInfo]:
    """Every parseable market of a raw markets payload (empty on a malformed body)."""
    cfg = config_with_defaults(config)
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        log.debug("polymarket_us markets payload is not JSON: %s", truncate(payload_text, 2048))
        return []
    records = market_records(payload)
    if not records:
        log.debug("polymarket_us markets payload has no markets: %s", truncate(payload_text, 2048))
    out = []
    for record in records:
        try:
            info = parse_market(record, cfg["fields"])
        except Exception as exc:  # noqa: BLE001 - one odd record must not fail the pass
            log.debug("polymarket_us market skipped (%s): %s", exc, truncate(json.dumps(record, default=str), 2048))
            continue
        if info is not None:
            out.append(info)
    return out


def parse_book(payload_text: str, now: Any = None) -> Book:
    """A Book from a raw book payload; SourceError when it is not a book at all."""
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError) as exc:
        log.debug("polymarket_us book payload is not JSON: %s", truncate(payload_text, 2048))
        raise SourceError("book payload is not JSON") from exc
    if isinstance(payload, dict):
        for key in ("book", "data", "orderbook"):
            if isinstance(payload.get(key), dict):
                payload = payload[key]
    if not isinstance(payload, dict) or ("bids" not in payload and "asks" not in payload):
        log.debug("polymarket_us book payload has no bids/asks: %s", truncate(payload_text, 2048))
        raise SourceError("book payload has no bids or asks")
    return Book(
        bids=clean_levels(payload.get("bids"), descending=True),
        asks=clean_levels(payload.get("asks"), descending=False),
        fetched_at=now or utcnow(),
    )


class PolymarketUSSource(MarketSource):
    name = PLATFORM

    def __init__(self, config: dict[str, Any] | None = None, timeout: float = 10.0) -> None:
        self.config = config_with_defaults(config)
        self.timeout = timeout

    def markets_url(self) -> str:
        url = self.config["base_url"].rstrip("/") + self.config["markets_path"]
        query = self.config.get("sport_query") or ""
        return f"{url}?{query}" if query else url

    def book_url(self, market_ref: str) -> str:
        return self.config["base_url"].rstrip("/") + self.config["book_path"].replace("{market_ref}", market_ref)

    def list_markets(self, games: list[dict[str, Any]], lookahead_days: int) -> list[MarketInfo]:
        status, text = http_get(self.markets_url(), self.timeout)
        check_status(status, text, "markets")
        return parse_markets(text, self.config)

    def fetch_book(self, market_ref: str) -> Book:
        status, text = http_get(self.book_url(market_ref), self.book_timeout)
        check_status(status, text, "book")
        return parse_book(text)

    def probe(self) -> dict[str, Any]:
        url = self.markets_url()
        status, text = http_get(url, self.timeout)
        return {"url": url, "status": status, "payload": truncate(text)}
