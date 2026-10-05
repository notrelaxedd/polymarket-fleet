"""Per-game signals (docs/ROBUSTNESS.md B2): quarterback change and players listed Out.

Pure functions shared by the sim (the games.csv adapter computes the quarterback
signals from the raw starter ids) and usable by the host when it builds the games feed.
A game's signals dict always has exactly SIGNAL_KEYS, each an int:

- home_qb_changed / away_qb_changed: the team's starting quarterback for this game
  differs from the last known starter of its previous games (across seasons). The
  first game of a team, and any game whose starter is unknown (an unplayed game),
  read 0.
- home_out_qb / away_out_qb, home_out_count / away_out_count: injury report rows with
  report_status "Out" whose date_modified is before the game's decision time; out_qb is
  1 when any of them plays QB. A player is counted once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

SIGNAL_KEYS = ("home_qb_changed", "away_qb_changed", "home_out_qb", "away_out_qb",
               "home_out_count", "away_out_count")
FLAG_KEYS = ("home_qb_changed", "away_qb_changed", "home_out_qb", "away_out_qb")
OUT_STATUS = "out"


def empty_signals() -> dict[str, int]:
    return {key: 0 for key in SIGNAL_KEYS}


def _count(value: Any) -> int:
    if value is None or value == "" or isinstance(value, (dict, list)):
        return 0
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def normalise_signals(value: Any) -> dict[str, int]:
    """Exactly SIGNAL_KEYS as non-negative ints (flags 0 or 1); missing or bad values are 0."""
    out = empty_signals()
    if not isinstance(value, dict):
        return out
    for key in SIGNAL_KEYS:
        number = _count(value.get(key))
        out[key] = min(number, 1) if key in FLAG_KEYS else number
    return out


def _qb_id(value: Any) -> str:
    return "" if value is None else str(value).strip()


def qb_changed_map(games: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """{game_id: {"home_qb_changed", "away_qb_changed"}} from rows carrying game_id,
    kickoff_at (sortable), home_team, away_team, home_qb_id and away_qb_id. Team codes
    must already be normalised so a relocated team keeps its history."""
    last: dict[str, str] = {}
    out: dict[str, dict[str, int]] = {}
    for game in sorted(games, key=lambda g: (str(g["kickoff_at"]), str(g["game_id"]))):
        flags: dict[str, int] = {}
        for side in ("home", "away"):
            team = str(game[f"{side}_team"])
            qb = _qb_id(game.get(f"{side}_qb_id"))
            previous = last.get(team)
            flags[f"{side}_qb_changed"] = 1 if qb and previous and qb != previous else 0
            if qb:
                last[team] = qb
        out[str(game["game_id"])] = flags
    return out


def parse_time(value: Any) -> datetime | None:
    """An aware UTC datetime from a datetime or an ISO string; None when absent or bad."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def decision_time(kickoff_at: Any, minutes_before: int | None) -> datetime | None:
    """Kickoff minus the decision lead (kickoff itself when the lead is unknown)."""
    kickoff = parse_time(kickoff_at)
    if kickoff is None:
        return None
    return kickoff - timedelta(minutes=max(0, int(minutes_before or 0)))


def out_signals(rows: Iterable[dict[str, Any]], decision_at: Any) -> tuple[int, int]:
    """(out_qb, out_count) of one team's injury rows for one game: status Out, modified
    strictly before decision_at. Rows without a usable date never count (no leakage)."""
    cutoff = parse_time(decision_at)
    if cutoff is None:
        return 0, 0
    players: dict[str, bool] = {}
    for row in rows:
        if str(row.get("report_status") or "").strip().lower() != OUT_STATUS:
            continue
        modified = parse_time(row.get("date_modified"))
        if modified is None or modified >= cutoff:
            continue
        key = _qb_id(row.get("gsis_id")) or _qb_id(row.get("full_name"))
        if not key:
            continue
        is_qb = str(row.get("position") or "").strip().upper() == "QB"
        players[key] = players.get(key, False) or is_qb
    return (1 if any(players.values()) else 0), len(players)


def injury_signals(home_rows: Iterable[dict[str, Any]], away_rows: Iterable[dict[str, Any]],
                   decision_at: Any) -> dict[str, int]:
    """The four injury keys of a game from its home and away injury rows."""
    home_qb, home_n = out_signals(home_rows, decision_at)
    away_qb, away_n = out_signals(away_rows, decision_at)
    return {"home_out_qb": home_qb, "away_out_qb": away_qb, "home_out_count": home_n, "away_out_count": away_n}
