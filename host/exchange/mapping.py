"""Map discovered markets to games and upsert `markets` rows (docs/TRADING.md).

A market maps to the game with the same two teams whose kickoff is within 36 h of the
market's start (or whose gameday matches). Confidence 1.0 and `mapping_confirmed` when
the teams and the gameday match exactly and the YES side is known; anything weaker is
stored unconfirmed for the owner to link by hand. A confirmed mapping is never changed
by discovery, only by the owner (`link_market`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.events import add_audit
from host.exchange.adapters import teams
from host.exchange.adapters.base import MarketInfo, MarketSource, utcnow
from host.nflverse import EASTERN
from host.settings import get_int_setting

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=36)
PAST_GRACE = timedelta(hours=6)


def upcoming_games(conn: psycopg.Connection, now: datetime, lookahead_days: int) -> list[dict[str, Any]]:
    """Games kicking off between 6 h ago and `lookahead_days` ahead (finals excluded)."""
    rows = conn.execute(
        """
        SELECT * FROM games
         WHERE kickoff_at >= %s AND kickoff_at <= %s AND status <> 'final'
         ORDER BY kickoff_at, game_id
        """,
        (now - PAST_GRACE, now + timedelta(days=lookahead_days)),
    ).fetchall()
    return [dict(r) for r in rows]


def candidate_games(conn: psycopg.Connection, home: str, away: str, kickoff: datetime | None, now: datetime, lookahead_days: int) -> list[dict[str, Any]]:
    """Games between the two teams (either orientation) near the market's start."""
    if kickoff is not None:
        gameday = kickoff.astimezone(EASTERN).date()
        rows = conn.execute(
            """
            SELECT * FROM games
             WHERE ((home_team = %(h)s AND away_team = %(a)s) OR (home_team = %(a)s AND away_team = %(h)s))
               AND ((kickoff_at BETWEEN %(lo)s AND %(hi)s) OR gameday = %(day)s)
             ORDER BY kickoff_at
            """,
            {"h": home, "a": away, "lo": kickoff - WINDOW, "hi": kickoff + WINDOW, "day": gameday},
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM games
             WHERE ((home_team = %(h)s AND away_team = %(a)s) OR (home_team = %(a)s AND away_team = %(h)s))
               AND kickoff_at BETWEEN %(lo)s AND %(hi)s AND status <> 'final'
             ORDER BY kickoff_at
            """,
            {"h": home, "a": away, "lo": now - PAST_GRACE, "hi": now + timedelta(days=lookahead_days)},
        ).fetchall()
    return [dict(r) for r in rows]


def match(conn: psycopg.Connection, info: MarketInfo, now: datetime, lookahead_days: int) -> dict[str, Any]:
    """{"game_id", "side", "confidence", "confirmed"} for a market; game_id None when
    the teams do not resolve to exactly one plausible game."""
    home, away = teams.resolve(info.home_team), teams.resolve(info.away_team)
    unmatched = {"game_id": None, "side": None, "confidence": 0.0, "confirmed": False}
    if home is None or away is None or home == away:
        return unmatched
    yes_team = {"home": home, "away": away}.get(info.side or "")
    candidates = candidate_games(conn, home, away, info.kickoff_at, now, lookahead_days)
    if info.kickoff_at is not None:
        day = info.kickoff_at.astimezone(EASTERN).date()
        exact = [g for g in candidates if g["gameday"] == day]
        candidates = exact or candidates
    if len(candidates) != 1:
        return unmatched
    game = candidates[0]
    side = None if yes_team is None else ("home" if yes_team == game["home_team"] else "away")
    if info.kickoff_at is None:
        confidence = 0.5
    elif game["gameday"] == info.kickoff_at.astimezone(EASTERN).date():
        confidence = 1.0
    else:
        confidence = 0.8
    if side is None:
        confidence = min(confidence, 0.5)
    return {"game_id": game["game_id"], "side": side, "confidence": confidence, "confirmed": confidence >= 1.0}


def _already_confirmed(conn: psycopg.Connection, info: MarketInfo, mapping: dict[str, Any]) -> bool:
    """True when another market of the same platform is already confirmed for this
    game and side: a second one (a spread or a duplicate listing) is left for the
    owner rather than traded as a second moneyline."""
    row = conn.execute(
        """
        SELECT 1 FROM markets WHERE platform = %s AND game_id = %s AND side = %s AND mapping_confirmed
           AND market_ref <> %s LIMIT 1
        """,
        (info.platform, mapping["game_id"], mapping["side"], info.market_ref),
    ).fetchone()
    return row is not None


def upsert_market(conn: psycopg.Connection, info: MarketInfo, mapping: dict[str, Any]) -> dict[str, Any]:
    """Insert or refresh a markets row; a confirmed mapping keeps game_id and side.
    Never auto-confirms a second market for one (platform, game, side)."""
    if mapping.get("confirmed") and _already_confirmed(conn, info, mapping):
        mapping = {**mapping, "confirmed": False, "confidence": min(float(mapping.get("confidence") or 0.0), 0.5)}
    row = conn.execute(
        """
        INSERT INTO markets (platform, market_ref, event_ref, title, game_id, side, mapping_confirmed,
                             mapping_confidence, tick, min_size, raw)
        VALUES (%(platform)s, %(ref)s, %(event)s, %(title)s, %(game_id)s, %(side)s, %(confirmed)s,
                %(confidence)s, %(tick)s, %(min_size)s, %(raw)s)
        ON CONFLICT (platform, market_ref) DO UPDATE SET
            event_ref = EXCLUDED.event_ref, title = EXCLUDED.title, tick = EXCLUDED.tick,
            min_size = EXCLUDED.min_size, raw = EXCLUDED.raw, updated_at = now(),
            game_id = CASE WHEN markets.mapping_confirmed THEN markets.game_id ELSE EXCLUDED.game_id END,
            side = CASE WHEN markets.mapping_confirmed THEN markets.side ELSE EXCLUDED.side END,
            mapping_confidence = CASE WHEN markets.mapping_confirmed THEN markets.mapping_confidence
                                      ELSE EXCLUDED.mapping_confidence END,
            mapping_confirmed = markets.mapping_confirmed OR EXCLUDED.mapping_confirmed
        RETURNING *
        """,
        {
            "platform": info.platform, "ref": info.market_ref, "event": info.event_ref, "title": info.title[:500],
            "game_id": mapping["game_id"], "side": mapping["side"], "confirmed": mapping["confirmed"],
            "confidence": mapping["confidence"], "tick": info.tick, "min_size": info.min_size,
            "raw": Jsonb(_jsonable(info.raw)),
        },
    ).fetchone()
    return dict(row)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def discover(conn: psycopg.Connection, source: MarketSource, now: datetime | None = None) -> dict[str, int]:
    """One discovery pass: list the source's markets for the games within the
    lookahead and upsert every one. Returns counts."""
    now = now or utcnow()
    lookahead = get_int_setting(conn, "market_lookahead_days", 8)
    games = upcoming_games(conn, now, lookahead)
    infos = source.list_markets(games, lookahead)
    counts = {"listed": len(infos), "confirmed": 0, "unmatched": 0, "upserted": 0}
    for info in infos:
        mapping = match(conn, info, now, lookahead)
        row = upsert_market(conn, info, mapping)
        counts["upserted"] += 1
        if row["mapping_confirmed"]:
            counts["confirmed"] += 1
        elif row["game_id"] is None:
            counts["unmatched"] += 1
    return counts


def link_market(conn: psycopg.Connection, market_id: Any, game_id: str, side: str, actor: str | None) -> dict[str, Any]:
    """The owner links an unmatched (or wrongly matched) market to a game by hand."""
    from host.errors import BadRequest, NotFound

    if side not in ("home", "away"):
        raise BadRequest("side must be home or away")
    game = conn.execute("SELECT game_id FROM games WHERE game_id = %s", (game_id,)).fetchone()
    if game is None:
        raise NotFound(f"unknown game {game_id}")
    before = conn.execute("SELECT * FROM markets WHERE id = %s FOR UPDATE", (market_id,)).fetchone()
    if before is None:
        raise NotFound(f"unknown market {market_id}")
    row = conn.execute(
        """
        UPDATE markets SET game_id = %s, side = %s, mapping_confirmed = true, mapping_confidence = 1.0,
               updated_at = now()
         WHERE id = %s RETURNING *
        """,
        (game_id, side, market_id),
    ).fetchone()
    add_audit(
        conn, "market_linked", str(market_id), actor,
        {"game_id": before["game_id"], "side": before["side"]}, {"game_id": game_id, "side": side},
    )
    return dict(row)
