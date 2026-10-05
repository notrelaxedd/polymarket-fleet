"""The body and ETag of GET /api/v1/data/games (docs/PROTOCOL.md "Data for workers").

Body: {"games": [... every game in the MODELS.md field set plus "signals" ...], "count":
n, "team_game_stats": [every team-game row sorted by kickoff_at, game_id, team],
"decision_minutes_before_kickoff": the injury cutoff the signals used}. Old workers
ignore the extra keys.

The injury signals count reports filed before kickoff minus the decision minutes: the
`?decision_minutes=N` query (0..300) when given, so a snapshot backtest gets exactly
the cutoff its params carry and never a report written after its bet, else the current
decision_minutes_before_kickoff setting.

ETag: the games stamp (`<count>-<max updated_at>`), the injuries and team_game_stats
stamps (same form) and `d<the decision minutes used>`, joined with dots. It changes when any of them does. The tag is computed
before the body, so a change racing the request can only make the client download again
next time, never keep a stale body.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg

from host import ingest_injuries, ingest_pbp, nflverse, signals
from host.errors import BadRequest


def feed_minutes(conn: psycopg.Connection, minutes: int | None = None) -> int:
    """The injury cutoff of a feed: `minutes` when given, else the setting."""
    return signals.decision_minutes(conn) if minutes is None else int(minutes)


def feed_etag(conn: psycopg.Connection, minutes: int | None = None) -> str:
    """`<games>.<injuries>.<team_game_stats>.d<decision minutes>`."""
    return ".".join((
        nflverse.games_etag(conn),
        ingest_injuries.injuries_stamp(conn),
        ingest_pbp.stats_stamp(conn),
        f"d{feed_minutes(conn, minutes)}",
    ))


def team_game_stats(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every team_game_stats row in the feed shape, by kickoff_at, game_id, team."""
    rows = conn.execute(
        f"SELECT * FROM ({signals.STATS_SELECT}) x ORDER BY x.kickoff_at NULLS LAST, x.game_id, x.team"
    ).fetchall()
    return [signals.stats_row(r) for r in rows]


def feed_games(conn: psycopg.Connection, minutes: int | None = None) -> list[dict[str, Any]]:
    """Every game (nflverse.worker_games) with its "signals" dict, injuries cut at
    kickoff minus `minutes` (the setting when None)."""
    games = nflverse.worker_games(conn)
    by_game = signals.signals_for(conn, None, feed_minutes(conn, minutes))
    for game in games:
        game["signals"] = by_game.get(game["game_id"]) or signals.empty_signals()
    return games


def feed(conn: psycopg.Connection, minutes: int | None = None) -> dict[str, Any]:
    """The feed body as a dict."""
    used = feed_minutes(conn, minutes)
    games = feed_games(conn, used)
    return {"games": games, "count": len(games), "team_game_stats": team_game_stats(conn),
            "decision_minutes_before_kickoff": used}


def dumps_feed(conn: psycopg.Connection, minutes: int | None = None) -> bytes:
    """The response body of GET /api/v1/data/games."""
    return json.dumps(feed(conn, minutes), separators=(",", ":")).encode("utf-8")


def parse_minutes(value: str | None) -> int | None:
    """The `decision_minutes` query: None when absent, else an integer 0..300 (400)."""
    if value is None:
        return None
    text = value.strip()
    if not text.isdigit() or not 0 <= int(text) <= signals.MAX_DECISION_MINUTES:
        raise BadRequest(f"decision_minutes must be an integer between 0 and {signals.MAX_DECISION_MINUTES}")
    return int(text)
