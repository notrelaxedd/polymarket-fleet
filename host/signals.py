"""Per-game signals served with the games feed and the trade state (docs/ROBUSTNESS.md B2).

game_signals(conn, game_ids) returns, per game id, {"signals": {...}, "team_stats":
{"home": [...], "away": [...]}} as the step 6 Part B contract describes; signals_for
gives the signals alone (every game when game_ids is None, for the games feed).

- home_qb_changed / away_qb_changed: the starting quarterback of this game (games.raw
  home_qb_id / away_qb_id) differs from the team's last known starter in its previous
  games, across seasons. A team's first game and a game whose starter is unknown read 0.
- home_out_qb / away_out_qb, home_out_count / away_out_count: `injuries` rows of the
  game's (season, game_type, week, team) with report_status Out whose date_modified is
  strictly before the decision time (kickoff minus the decision_minutes_before_kickoff
  setting, or the minutes a snapshot backtest asks the games feed for). A row modified at or after it, or without a date, never counts, so a
  backtest cannot see a report written after the bet would have been placed.
- team_stats: the team's team_game_stats rows strictly before this game's kickoff
  with both EPA values, oldest first, at most TEAM_STATS_LIMIT.

The rules are the pure functions of fleet.sim.signals, so the host and the sim agree.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import psycopg

from fleet.sim.signals import decision_time, empty_signals, injury_signals, qb_changed_map
from host.settings import get_int_setting

TEAM_STATS_LIMIT = 16
DEFAULT_DECISION_MINUTES, MAX_DECISION_MINUTES = 60, 300
STATS_COLUMNS = ("game_id", "season", "week", "team", "kickoff_at", "off_epa_per_play", "def_epa_per_play",
                 "pass_rate", "plays", "success_rate")
STATS_SELECT = (
    "SELECT s.game_id, s.season, s.week, s.team, COALESCE(g.kickoff_at, s.kickoff_at) AS kickoff_at,"
    " s.off_epa_per_play, s.def_epa_per_play, s.pass_rate, s.plays, s.success_rate"
    " FROM team_game_stats s LEFT JOIN games g ON g.game_id = s.game_id"
)


def iso(value: Any) -> Any:
    """A datetime as an ISO UTC string with a Z suffix; anything else unchanged."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def stats_row(row: dict[str, Any]) -> dict[str, Any]:
    """A team_game_stats row in the feed shape (exactly STATS_COLUMNS)."""
    return {key: iso(row[key]) for key in STATS_COLUMNS}


def decision_minutes(conn: psycopg.Connection) -> int:
    """The decision_minutes_before_kickoff setting (0..300, default 60)."""
    return max(0, min(MAX_DECISION_MINUTES, get_int_setting(conn, "decision_minutes_before_kickoff", DEFAULT_DECISION_MINUTES)))


def _games(conn: psycopg.Connection, game_ids: list[str] | None) -> list[dict[str, Any]]:
    sql = ("SELECT game_id, season, game_type, week, kickoff_at, home_team, away_team,"
           " raw->>'home_qb_id' AS home_qb_id, raw->>'away_qb_id' AS away_qb_id FROM games")
    if game_ids is None:
        return conn.execute(sql).fetchall()
    return conn.execute(sql + " WHERE game_id = ANY(%s)", (list(game_ids),)).fetchall()


def _team_history(conn: psycopg.Connection, games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every game of the teams playing in `games` (enough for their qb_changed flags)."""
    teams = sorted({g["home_team"] for g in games} | {g["away_team"] for g in games})
    return conn.execute(
        "SELECT game_id, kickoff_at, home_team, away_team, raw->>'home_qb_id' AS home_qb_id,"
        " raw->>'away_qb_id' AS away_qb_id FROM games WHERE home_team = ANY(%s) OR away_team = ANY(%s)",
        (teams, teams),
    ).fetchall()


def _out_rows(conn: psycopg.Connection, games: list[dict[str, Any]] | None) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    """Out rows grouped by (season, game_type, week, team); all of them when games is None."""
    sql = ("SELECT season, game_type, week, team, gsis_id, full_name, position, report_status, date_modified"
           " FROM injuries WHERE lower(report_status) = 'out'")
    if games is None:
        rows = conn.execute(sql).fetchall()
    else:
        seasons = sorted({int(g["season"]) for g in games})
        teams = sorted({g["home_team"] for g in games} | {g["away_team"] for g in games})
        rows = conn.execute(sql + " AND season = ANY(%s) AND team = ANY(%s)", (seasons, teams)).fetchall()
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["season"], row["game_type"], row["week"], row["team"])].append(row)
    return grouped


def _qb_input(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "kickoff_at": iso(row["kickoff_at"])}


def signals_for(
    conn: psycopg.Connection, game_ids: list[str] | None = None, minutes: int | None = None
) -> dict[str, dict[str, int]]:
    """{game_id: signals} for the given games (every game when game_ids is None), with
    injury reports cut at kickoff minus `minutes` (the setting when None)."""
    games = _games(conn, game_ids)
    if not games:
        return {}
    history = games if game_ids is None else _team_history(conn, games)
    qb = qb_changed_map(_qb_input(g) for g in history)
    outs = _out_rows(conn, None if game_ids is None else games)
    if minutes is None:
        minutes = decision_minutes(conn)
    result: dict[str, dict[str, int]] = {}
    for game in games:
        decide = decision_time(game["kickoff_at"], minutes)
        key = (game["season"], game["game_type"], game["week"])
        signals = empty_signals()
        signals.update(qb.get(game["game_id"], {}))
        signals.update(injury_signals(outs.get(key + (game["home_team"],), []),
                                      outs.get(key + (game["away_team"],), []), decide))
        result[game["game_id"]] = signals
    return result


def team_stats_before(conn: psycopg.Connection, team: str, kickoff: Any, limit: int = TEAM_STATS_LIMIT) -> list[dict[str, Any]]:
    """The team's stats rows strictly before `kickoff`, oldest first, at most `limit`.
    A row without both EPA values is skipped, as the worker's games cache drops it
    (fleet.sim.data.normalise_stat_row), so live and backtested rolling EPA average the
    same games."""
    if kickoff is None:
        return []
    rows = conn.execute(
        f"SELECT * FROM ({STATS_SELECT}) x WHERE x.team = %s AND x.kickoff_at < %s"
        " AND x.off_epa_per_play IS NOT NULL AND x.def_epa_per_play IS NOT NULL"
        " ORDER BY x.kickoff_at DESC, x.game_id DESC LIMIT %s",
        (team, kickoff, limit),
    ).fetchall()
    return [stats_row(r) for r in reversed(rows)]


def game_signals(conn: psycopg.Connection, game_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Signals and prior team stats per known game id (unknown ids are left out)."""
    ids = [str(g) for g in game_ids]
    if not ids:
        return {}
    signals = signals_for(conn, ids)
    kickoffs = conn.execute(
        "SELECT game_id, kickoff_at, home_team, away_team FROM games WHERE game_id = ANY(%s)", (ids,)
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for game in kickoffs:
        out[game["game_id"]] = {
            "signals": signals.get(game["game_id"], empty_signals()),
            "team_stats": {side: team_stats_before(conn, game[f"{side}_team"], game["kickoff_at"])
                           for side in ("home", "away")},
        }
    return out
