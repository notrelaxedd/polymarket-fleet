"""Snapshot retention: roll raw rows older than `snapshot_retention_days` up into
1-minute `price_bars`, then delete them. Idempotent: a bar merges with what is already
stored and the rows are gone once rolled up. Closing prices are frozen first and bets
reference nothing here, so neither changes. Snapshots an order cites stay (orders
reference them). The same pass deletes `game_state` rows older than the same number of
days except each game's newest row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import psycopg

from host.exchange import snapshots
from host.exchange.adapters.base import utcnow
from host.settings import get_int_setting

log = logging.getLogger(__name__)


def rollup(conn: psycopg.Connection, before: datetime) -> int:
    """Merge every snapshot older than `before` into its minute bar; rows merged."""
    row = conn.execute(
        """
        WITH src AS (
          SELECT market_id, date_trunc('minute', ts) AS minute, ts, id, mid, bid, ask, liquidity_usd_cents
            FROM price_snapshots s WHERE s.ts < %s AND s.mid IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.snapshot_id = s.id)
        ), agg AS (
          SELECT market_id, minute,
                 (array_agg(mid ORDER BY ts, id))[1] AS open,
                 max(mid) AS high, min(mid) AS low,
                 (array_agg(mid ORDER BY ts DESC, id DESC))[1] AS close,
                 (array_agg(bid ORDER BY ts DESC, id DESC))[1] AS bid,
                 (array_agg(ask ORDER BY ts DESC, id DESC))[1] AS ask,
                 min(liquidity_usd_cents) AS min_liq, count(*) AS n
            FROM src GROUP BY market_id, minute
        )
        INSERT INTO price_bars (market_id, minute, open, high, low, close, bid, ask, min_liquidity_usd_cents, n)
        SELECT market_id, minute, open, high, low, close, bid, ask, min_liq, n FROM agg
        ON CONFLICT (market_id, minute) DO UPDATE SET
            high = GREATEST(price_bars.high, EXCLUDED.high),
            low = LEAST(price_bars.low, EXCLUDED.low),
            close = EXCLUDED.close, bid = EXCLUDED.bid, ask = EXCLUDED.ask,
            min_liquidity_usd_cents = LEAST(price_bars.min_liquidity_usd_cents, EXCLUDED.min_liquidity_usd_cents),
            n = price_bars.n + EXCLUDED.n
        RETURNING n
        """,
        (before,),
    ).fetchall()
    return len(row)


def delete_raw(conn: psycopg.Connection, before: datetime) -> int:
    """Delete snapshots older than `before` that no order cites."""
    rows = conn.execute(
        """
        DELETE FROM price_snapshots s WHERE s.ts < %s
           AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.snapshot_id = s.id)
        RETURNING s.id
        """,
        (before,),
    ).fetchall()
    return len(rows)


def prune_game_states(conn: psycopg.Connection, before: datetime) -> int:
    """Delete game_state rows older than `before` except each game's newest row (the
    feed writes one every 3 to 5 s per live game); rows deleted.

    Nothing reads an old row: the feed polls a game only within 8 hours of kickoff
    (gamestate.LIVE_WINDOW, under the 1-day minimum) and the newest row (latest_state,
    the feed's "final since kickoff" check) stays. Settlement (settle_sells.state_at,
    every 30 s once the game is final) reads the `state_at_entry` the order's approval
    event recorded (host.trading.limits writes one for every in-game approval), and
    only without one the newest row at or before the order: hours after the order,
    long before this window."""
    rows = conn.execute(
        """
        DELETE FROM game_state s WHERE s.ts < %s
           AND EXISTS (SELECT 1 FROM game_state n WHERE n.game_id = s.game_id AND (n.ts, n.id) > (s.ts, s.id))
        RETURNING s.id
        """,
        (before,),
    ).fetchall()
    return len(rows)


def run(conn: psycopg.Connection, now: datetime | None = None, days: int | None = None) -> dict[str, Any]:
    """One retention pass: freeze closing prices, roll up, delete, prune game states.
    Safe to repeat."""
    now = now or utcnow()
    days = get_int_setting(conn, "snapshot_retention_days", 14) if days is None else days
    before = now - timedelta(days=days)
    frozen = snapshots.freeze_closing_prices(conn, now)
    bars = rollup(conn, before)
    deleted = delete_raw(conn, before)
    states = prune_game_states(conn, before)
    if bars or deleted or states:
        log.info("retention: %d bars merged, %d raw snapshots and %d game states deleted before %s",
                 bars, deleted, states, before.isoformat())
    return {"before": before, "bars": bars, "deleted": deleted, "game_states": states, "closing_frozen": frozen}
