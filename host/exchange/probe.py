"""`POST /api/exchange/probe`: the configured source's raw markets payload, truncated
to 64 KiB, so the owner can paste it back when the unverified Polymarket shapes
turn out different. `probe_gamestate` does the same for the live game-state feed
(ESPN summary, or Yahoo) and adds what the parser extracted."""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from host.api.serialize import jsonable
from host.exchange import mapping
from host.exchange.adapters import source_from_settings
from host.exchange.adapters.base import truncate, utcnow
from host.settings import get_int_setting, get_setting

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


def probe_gamestate(conn: psycopg.Connection | None, event_id: str, yahoo: bool = False, url: str | None = None,
                    fetch: Any = None) -> dict[str, Any]:
    """One game-state request for an ESPN event id (or Yahoo with `yahoo`):
    {"source", "event_id", "game_id", "url", "status", "payload" (first 64 KiB),
    "parsed", "error"}. `url` overrides the settings template (`{event_id}` is filled
    in). Without a database the default ESPN template is used. Never raises."""
    from host.exchange.gamestate import DEFAULT_SUMMARY_URL, default_fetch, parse_summary

    source = "yahoo" if yahoo else "espn_summary"
    out: dict[str, Any] = {"source": source, "event_id": event_id, "game_id": None, "url": None, "status": None,
                           "payload": None, "parsed": None, "error": None}
    try:
        template = url
        if template is None and conn is not None:
            template = get_setting(conn, "yahoo_pbp_url" if yahoo else "espn_summary_url", None)
        if template is None and not yahoo:
            template = DEFAULT_SUMMARY_URL
        if not template:
            out["error"] = "yahoo_pbp_url is not set: pass --url with the Yahoo play-by-play URL ({event_id} is filled in)"
            return out
        if conn is not None:
            row = conn.execute("SELECT game_id FROM games WHERE raw->>'espn' = %s", (str(event_id),)).fetchone()
            out["game_id"] = row["game_id"] if row else None
        out["url"] = str(template).replace("{event_id}", str(event_id))
        status, text = (fetch or default_fetch)(out["url"])
        out["status"] = status
        out["payload"] = truncate(text, LIMIT)
        if yahoo:
            out["parsed"] = "no Yahoo parser yet: paste this payload back so one can be written"
        else:
            out["parsed"] = jsonable(parse_summary(text))
            if status == 200 and not out["parsed"]:
                out["error"] = "the summary parser extracted nothing from this payload"
    except Exception as exc:  # noqa: BLE001 - the owner wants the error text, not a traceback
        log.warning("game-state probe failed: %s", exc)
        out["error"] = str(exc)
    return out
