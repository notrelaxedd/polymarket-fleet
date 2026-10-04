"""Snapshot retention: roll raw rows older than `snapshot_retention_days` up into
1-minute `price_bars`, then delete them. Idempotent: a bar merges with what is already
stored and the rows are gone once rolled up. Closing prices are frozen first and bets
reference nothing here, so neither changes. Snapshots an order cites stay (orders
reference them).
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


def run(conn: psycopg.Connection, now: datetime | None = None, days: int | None = None) -> dict[str, Any]:
    """One retention pass: freeze closing prices, roll up, delete. Safe to repeat."""
    now = now or utcnow()
    days = get_int_setting(conn, "snapshot_retention_days", 14) if days is None else days
    before = now - timedelta(days=days)
    frozen = snapshots.freeze_closing_prices(conn, now)
    bars = rollup(conn, before)
    deleted = delete_raw(conn, before)
    if bars or deleted:
        log.info("retention: %d bars merged, %d raw snapshots deleted before %s", bars, deleted, before.isoformat())
    return {"before": before, "bars": bars, "deleted": deleted, "closing_frozen": frozen}
