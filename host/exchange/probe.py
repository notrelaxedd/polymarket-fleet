"""`POST /api/exchange/probe`: the configured source's raw markets payload, truncated
to 64 KiB, so the owner can paste it back when the unverified Polymarket shapes
turn out different."""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from host.exchange import mapping
from host.exchange.adapters import source_from_settings
from host.exchange.adapters.base import truncate, utcnow
from host.settings import get_int_setting

log = logging.getLogger(__name__)

LIMIT = 64 * 1024


def probe_markets(conn: psycopg.Connection) -> dict[str, Any]:
    """{"source", "url", "status", "payload", "error"}; never raises."""
    out: dict[str, Any] = {"source": None, "url": None, "status": None, "payload": None, "error": None}
    try:
        source = source_from_settings(conn)
        out["source"] = source.name
        if source.name == "sim":
            from host.exchange.adapters.sim import SimSource

            lookahead = get_int_setting(conn, "market_lookahead_days", 8)
            games = mapping.upcoming_games(conn, utcnow(), lookahead)
            source = SimSource(games)
        result = source.probe()
        out.update({k: result.get(k) for k in ("url", "status", "payload")})
        out["payload"] = truncate(out["payload"], LIMIT)
    except Exception as exc:  # noqa: BLE001 - the owner wants the error text, not a 500
        log.warning("probe failed: %s", exc)
        out["error"] = str(exc)
    return out
