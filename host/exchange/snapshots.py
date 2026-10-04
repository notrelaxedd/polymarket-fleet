"""Price snapshots: the poller with its two cadences, liquidity near the touch, the
mirrored best bid/ask on `markets` and the closing price frozen at kickoff.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.exchange.adapters.base import Book, MarketSource, RateLimited, SourceError, utcnow
from host.exchange.ratelimit import RateLimiter
from host.settings import get_int_setting

log = logging.getLogger(__name__)

TOUCH_BAND = 0.05
MAX_LEVELS = 10
PASS_BUDGET_S = 5.0


def liquidity_usd_cents(book: Book, band: float = TOUCH_BAND) -> int:
    """Dollar depth (cents) within `band` of the touch on both sides."""
    total = 0.0
    bid, ask = book.best_bid, book.best_ask
    if bid is not None:
        total += sum(p * s for p, s in book.bids if p >= bid - band - 1e-9)
    if ask is not None:
        total += sum(p * s for p, s in book.asks if p <= ask + band + 1e-9)
    return int(round(total * 100))


def record_snapshot(conn: psycopg.Connection, market_id: Any, book: Book, now: datetime | None = None) -> dict[str, Any]:
    """Store one snapshot and mirror its touch on the market row."""
    ts = now or book.fetched_at or utcnow()
    liquidity = liquidity_usd_cents(book)
    mid = book.mid
    row = conn.execute(
        """
        INSERT INTO price_snapshots (market_id, ts, bid, ask, mid, bid_depth, ask_depth, liquidity_usd_cents)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            market_id, ts, book.best_bid, book.best_ask, None if mid is None else round(mid, 4),
            Jsonb(book.bids[:MAX_LEVELS]), Jsonb(book.asks[:MAX_LEVELS]), liquidity,
        ),
    ).fetchone()
    conn.execute(
        """
        UPDATE markets SET best_bid = %s, best_ask = %s, liquidity_usd_cents = %s, last_snapshot_at = %s,
               updated_at = now()
         WHERE id = %s
        """,
        (book.best_bid, book.best_ask, liquidity, ts, market_id),
    )
    return dict(row)


def latest_snapshot(conn: psycopg.Connection, market_id: Any) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM price_snapshots WHERE market_id = %s ORDER BY ts DESC, id DESC LIMIT 1", (market_id,)
    ).fetchone()
    return None if row is None else dict(row)


def due_markets(conn: psycopg.Connection, now: datetime, active_s: int, idle_s: int, lookahead_days: int) -> list[dict[str, Any]]:
    """Mapped, unresolved markets of games within the lookahead whose last snapshot is
    older than their cadence (2 s with an active assignment, else 30 s)."""
    rows = conn.execute(
        """
        SELECT m.*, g.kickoff_at,
               EXISTS (SELECT 1 FROM assignments a WHERE a.game_id = m.game_id AND a.status = 'active') AS active
          FROM markets m JOIN games g ON g.game_id = m.game_id
         WHERE m.status <> 'resolved' AND g.status <> 'final'
           AND g.kickoff_at <= %s AND g.kickoff_at >= %s
         ORDER BY active DESC, m.last_snapshot_at NULLS FIRST
        """,
        (now + timedelta(days=lookahead_days), now - timedelta(hours=6)),
    ).fetchall()
    due = []
    for row in rows:
        cadence = active_s if row["active"] else idle_s
        last = row["last_snapshot_at"]
        if last is None or (now - last).total_seconds() >= cadence:
            due.append(dict(row))
    return due


def poll(
    conn: psycopg.Connection,
    source: MarketSource,
    limiter: RateLimiter | None = None,
    now: datetime | None = None,
    budget_s: float = PASS_BUDGET_S,
) -> dict[str, Any]:
    """Fetch and store the book of every due market, one market-data token each.

    A pass stops fetching after `budget_s` of wall clock (the rest is due again next
    time) so a slow source cannot hold the exchange loop. Each snapshot is stamped
    with the time its fetch returned (`now` plus the whole seconds of wall clock the
    pass had used), not the pass start, so a book fetched after kickoff during a long
    pass never passes as a pre-kickoff closing price. A 429 halves the market-data rate for 60 s. The
    counts carry `error`: the last failure when any fetch failed (every fetch
    failing is a hard error the loop reports), else None.
    """
    now = now or utcnow()
    started = time.monotonic()
    active_s = get_int_setting(conn, "snapshot_active_s", 2)
    idle_s = get_int_setting(conn, "snapshot_idle_s", 30)
    lookahead = get_int_setting(conn, "market_lookahead_days", 8)
    counts: dict[str, Any] = {"due": 0, "stored": 0, "throttled": 0, "failed": 0, "deferred": 0, "error": None}
    due = due_markets(conn, now, active_s, idle_s, lookahead)
    counts["due"] = len(due)
    for market in due:
        if time.monotonic() - started > budget_s:
            counts["deferred"] += 1
            continue
        if limiter is not None and not limiter.take("market_data", now=now):
            counts["throttled"] += 1
            continue
        try:
            book = source.fetch_book(market["market_ref"])
        except RateLimited as exc:
            counts["failed"] += 1
            counts["error"] = f"{market['market_ref']}: {exc}"
            if limiter is not None:
                limiter.on_429("market_data", now=now)
            log.warning("snapshot of %s rate limited: %s", market["market_ref"], exc)
            continue
        except Exception as exc:  # noqa: BLE001 - one bad market must not stop the poll
            counts["failed"] += 1
            counts["error"] = f"{market['market_ref']}: {exc}"
            log.warning("snapshot of %s failed: %s", market["market_ref"], exc)
            continue
        fetched = now + timedelta(seconds=int(time.monotonic() - started))
        record_snapshot(conn, market["id"], book, fetched)
        counts["stored"] += 1
    freeze_closing_prices(conn, now)
    if counts["failed"] and not counts["stored"]:
        raise SourceError(f"every book fetch failed ({counts['failed']} of {counts['due']}): {counts['error']}")
    if counts["error"]:
        counts["error"] = f"{counts['failed']} of {counts['due']} book fetches failed, last: {counts['error']}"
    return counts


def freeze_closing_prices(conn: psycopg.Connection, now: datetime | None = None) -> int:
    """Set `closing_price` for markets whose game has kicked off: the mid of the last
    snapshot strictly before kickoff, else the last snapshot. Never changed later."""
    now = now or utcnow()
    rows = conn.execute(
        """
        SELECT m.id, g.kickoff_at FROM markets m JOIN games g ON g.game_id = m.game_id
         WHERE m.closing_price IS NULL AND g.kickoff_at IS NOT NULL AND g.kickoff_at <= %s
        """,
        (now,),
    ).fetchall()
    frozen = 0
    for row in rows:
        price = closing_price_for(conn, row["id"], row["kickoff_at"])
        if price is None:
            continue
        conn.execute("UPDATE markets SET closing_price = %s, updated_at = now() WHERE id = %s AND closing_price IS NULL", (price, row["id"]))
        frozen += 1
    return frozen


def freeze_game_closing_prices(conn: psycopg.Connection, game_id: str) -> int:
    """At settlement: freeze the closing price of every market of one game that still
    lacks one, whatever its kickoff says. A final game was played, so a kickoff still
    in the future (a simulated final) only means the fallback applies: the mid of the
    last snapshot. Never changes a price already frozen."""
    rows = conn.execute(
        """
        SELECT m.id, g.kickoff_at FROM markets m JOIN games g ON g.game_id = m.game_id
         WHERE m.game_id = %s AND m.closing_price IS NULL
        """,
        (game_id,),
    ).fetchall()
    frozen = 0
    for row in rows:
        price = closing_price_for(conn, row["id"], row["kickoff_at"] or utcnow())
        if price is None:
            continue
        conn.execute("UPDATE markets SET closing_price = %s, updated_at = now() WHERE id = %s AND closing_price IS NULL", (price, row["id"]))
        frozen += 1
    return frozen


def closing_price_for(conn: psycopg.Connection, market_id: Any, kickoff_at: datetime) -> float | None:
    row = conn.execute(
        """
        SELECT mid FROM price_snapshots WHERE market_id = %s AND ts < %s AND mid IS NOT NULL
         ORDER BY ts DESC, id DESC LIMIT 1
        """,
        (market_id, kickoff_at),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT mid FROM price_snapshots WHERE market_id = %s AND mid IS NOT NULL ORDER BY ts DESC, id DESC LIMIT 1",
            (market_id,),
        ).fetchone()
    return None if row is None else float(row["mid"])
