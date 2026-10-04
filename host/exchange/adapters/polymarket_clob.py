"""Polymarket offshore public API as a price source only (never for orders).

Gamma `GET {gamma_url}/events?tag_slug={tag_slug}&closed=false&limit=200` answers a
list of events, each with `title` ("Chiefs vs. Raiders"), `startDate` and `markets`;
each market carries `id`, `question`, `outcomes` and `clobTokenIds` (JSON-encoded
lists), `gameStartTime`/`endDate`, `orderPriceMinTickSize`, `orderMinSize`. One YES
contract per (market, outcome): the market_ref is the outcome's CLOB token id. CLOB
`GET {clob_url}/book?token_id=...` answers `{"bids": [{price, size}], "asks": [...]}`.
Parsing is defensive; unknown shapes are logged at DEBUG with the payload truncated.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from host.exchange.adapters import teams
from host.exchange.adapters.base import (
    Book, MarketInfo, MarketSource, SourceError, check_status, clamp_min_size, clean_levels, http_get, parse_time,
    truncate, utcnow,
)

log = logging.getLogger(__name__)

PLATFORM = "polymarket_clob"
DEFAULTS: dict[str, Any] = {
    "gamma_url": "https://gamma-api.polymarket.com",
    "clob_url": "https://clob.polymarket.com",
    "tag_slug": "nfl",
    "limit": 200,
}
YES_WORDS = ("yes",)
NO_WORDS = ("no",)
MONEYLINE_TYPES = ("moneyline", "money_line", "winner", "h2h")
# Questions of markets that are not a straight winner contract: spreads, totals,
# halves and quarters, props. Only used when the market carries no sportsMarketType.
NOT_MONEYLINE = re.compile(
    r"spread|handicap|\bo/u\b|over/under|\bover\b|\bunder\b|\btotal|\(\s*[+-]\d|[+-]\d+(\.\d+)?\)|"
    r"\b(1st|2nd|3rd|4th|first|second)\s+(half|quarter)\b|\bhalf\b|\bquarter\b|\bprop\b|\bmvp\b",
    re.IGNORECASE,
)


def _json_list(value: Any) -> list[Any]:
    """A list that may arrive JSON-encoded as a string (Gamma does that)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def event_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        for key in ("events", "data", "results"):
            if isinstance(payload.get(key), list):
                return [e for e in payload[key] if isinstance(e, dict)]
    return []


def _event_teams(event: dict[str, Any], market: dict[str, Any]) -> tuple[str | None, str | None]:
    """(home, away) from the event title "Away vs. Home" or the market's question;
    Gamma lists the away team first in "A vs. B" titles."""
    for text in (event.get("title"), market.get("question"), event.get("slug")):
        found = teams.find(str(text)) if text else []
        if len(found) == 2:
            return found[1], found[0]
    return None, None


def parse_event(event: dict[str, Any]) -> list[MarketInfo]:
    """Every YES contract of one Gamma event (two per moneyline market)."""
    out: list[MarketInfo] = []
    markets = event.get("markets")
    if not isinstance(markets, list):
        log.debug("polymarket_clob event without markets: %s", truncate(json.dumps(event), 2048))
        return out
    for market in markets:
        if not isinstance(market, dict):
            continue
        try:
            out.extend(parse_market(event, market))
        except Exception as exc:  # noqa: BLE001 - one odd record must not fail the pass
            log.debug("polymarket_clob market skipped (%s): %s", exc, truncate(json.dumps(market, default=str), 2048))
    return out


def is_moneyline(market: dict[str, Any]) -> bool:
    """Only straight winner markets are tradable contracts here: `sportsMarketType`
    decides when present, else the question must not read like a spread, total,
    half, quarter or prop."""
    kind = market.get("sportsMarketType")
    if isinstance(kind, str) and kind.strip():
        return kind.strip().lower() in MONEYLINE_TYPES
    question = str(market.get("question") or "")
    return not NOT_MONEYLINE.search(question)


def parse_market(event: dict[str, Any], market: dict[str, Any]) -> list[MarketInfo]:
    outcomes = [str(o) for o in _json_list(market.get("outcomes"))]
    tokens = [str(t) for t in _json_list(market.get("clobTokenIds"))]
    if not outcomes or len(outcomes) != len(tokens):
        log.debug("polymarket_clob market without outcome tokens: %s", truncate(json.dumps(market), 2048))
        return []
    if market.get("closed") is True or market.get("active") is False:
        return []
    if not is_moneyline(market):
        log.debug("polymarket_clob market is not a moneyline, skipped: %s", market.get("question"))
        return []
    home, away = _event_teams(event, market)
    kickoff = parse_time(market.get("gameStartTime")) or parse_time(event.get("startDate")) or parse_time(market.get("endDate"))
    tick = _float(market.get("orderPriceMinTickSize"), 0.01)
    min_size = clamp_min_size(market.get("orderMinSize"))
    question = str(market.get("question") or event.get("title") or market.get("id") or "")
    out: list[MarketInfo] = []
    for outcome, token in zip(outcomes, tokens):
        team = _outcome_team(outcome, question, home, away)
        if team is None:
            continue
        side = "home" if team == home else "away" if team == away else None
        out.append(MarketInfo(
            platform=PLATFORM,
            market_ref=token,
            event_ref=str(event.get("id")) if event.get("id") is not None else None,
            title=f"{question} [{outcome}]",
            home_team=home,
            away_team=away,
            side=side,
            kickoff_at=kickoff,
            tick=tick if 0 < tick < 1 else 0.01,
            min_size=min_size,
            raw={"event_id": event.get("id"), "market_id": market.get("id"), "outcome": outcome, "question": question},
        ))
    return out


def _outcome_team(outcome: str, question: str, home: str | None, away: str | None) -> str | None:
    """The team an outcome pays: the outcome's own name, or for Yes/No questions the
    first team named in the question ("Will X beat Y?"); No pays the other team."""
    lowered = outcome.strip().lower()
    if lowered in YES_WORDS or lowered in NO_WORDS:
        named = teams.find(question)
        if not named or home is None or away is None:
            return None
        subject = named[0]
        if lowered in YES_WORDS:
            return subject
        return away if subject == home else home
    return teams.resolve(outcome)


def parse_events(payload_text: str) -> list[MarketInfo]:
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        log.debug("polymarket_clob events payload is not JSON: %s", truncate(payload_text, 2048))
        return []
    events = event_records(payload)
    if not events:
        log.debug("polymarket_clob events payload has no events: %s", truncate(payload_text, 2048))
    out: list[MarketInfo] = []
    for event in events:
        out.extend(parse_event(event))
    return out


def parse_book(payload_text: str, now: Any = None) -> Book:
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError) as exc:
        log.debug("polymarket_clob book payload is not JSON: %s", truncate(payload_text, 2048))
        raise SourceError("book payload is not JSON") from exc
    if not isinstance(payload, dict) or ("bids" not in payload and "asks" not in payload):
        log.debug("polymarket_clob book payload has no bids/asks: %s", truncate(payload_text, 2048))
        raise SourceError("book payload has no bids or asks")
    return Book(
        bids=clean_levels(payload.get("bids"), descending=True),
        asks=clean_levels(payload.get("asks"), descending=False),
        fetched_at=now or utcnow(),
    )


class PolymarketClobSource(MarketSource):
    name = PLATFORM

    def __init__(self, config: dict[str, Any] | None = None, timeout: float = 10.0) -> None:
        self.config = {**DEFAULTS, **(config or {})}
        self.timeout = timeout

    def events_url(self) -> str:
        base = str(self.config["gamma_url"]).rstrip("/")
        return f"{base}/events?tag_slug={self.config['tag_slug']}&closed=false&limit={int(self.config['limit'])}"

    def book_url(self, market_ref: str) -> str:
        return f"{str(self.config['clob_url']).rstrip('/')}/book?token_id={market_ref}"

    def list_markets(self, games: list[dict[str, Any]], lookahead_days: int) -> list[MarketInfo]:
        status, text = http_get(self.events_url(), self.timeout)
        check_status(status, text, "events")
        return parse_events(text)

    def fetch_book(self, market_ref: str) -> Book:
        status, text = http_get(self.book_url(market_ref), self.book_timeout)
        check_status(status, text, "book")
        return parse_book(text)

    def probe(self) -> dict[str, Any]:
        url = self.events_url()
        status, text = http_get(url, self.timeout)
        return {"url": url, "status": status, "payload": truncate(text)}
