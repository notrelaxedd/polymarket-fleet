"""The body and ETag of GET /api/v1/data/games (docs/PROTOCOL.md "Data for workers").

Body: {"games": [... every game in the MODELS.md field set plus "signals" ...], "count":
n, "team_game_stats": [every team-game row sorted by kickoff_at, game_id, team]}. Old
workers ignore the extra keys.

ETag: the games stamp (`<count>-<max updated_at>`), the injuries and team_game_stats
stamps (same form) and the decision_minutes_before_kickoff setting (the injury signals
depend on it), joined with dots. It changes when any of them does. The tag is computed
before the body, so a change racing the request can only make the client download again
next time, never keep a stale body.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg

from host import ingest_injuries, ingest_pbp, nflverse, signals


def feed_etag(conn: psycopg.Connection) -> str:
    """`<games>.<injuries>.<team_game_stats>.d<decision minutes>`."""
    return ".".join((
        nflverse.games_etag(conn),
        ingest_injuries.injuries_stamp(conn),
        ingest_pbp.stats_stamp(conn),
        f"d{signals.decision_minutes(conn)}",
    ))


def team_game_stats(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every team_game_stats row in the feed shape, by kickoff_at, game_id, team."""
    rows = conn.execute(
        f"SELECT * FROM ({signals.STATS_SELECT}) x ORDER BY x.kickoff_at NULLS LAST, x.game_id, x.team"
    ).fetchall()
    return [signals.stats_row(r) for r in rows]


def feed_games(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every game (nflverse.worker_games) with its "signals" dict."""
    games = nflverse.worker_games(conn)
    by_game = signals.signals_for(conn, None)
    for game in games:
        game["signals"] = by_game.get(game["game_id"]) or signals.empty_signals()
    return games


def feed(conn: psycopg.Connection) -> dict[str, Any]:
    """The feed body as a dict."""
    games = feed_games(conn)
    return {"games": games, "count": len(games), "team_game_stats": team_game_stats(conn)}


def dumps_feed(conn: psycopg.Connection) -> bytes:
    """The response body of GET /api/v1/data/games."""
    return json.dumps(feed(conn), separators=(",", ":")).encode("utf-8")
