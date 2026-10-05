"""Load games for the sim: the agent's JSON cache (GET /api/v1/data/games) or the raw
nflverse games.csv (tests load the fixture directly through the csv adapter).

Every row becomes a plain dict with the fields listed in docs/MODELS.md, team codes
normalised for continuity, season and week as ints, scores and moneylines as ints or
None, sorted by kickoff then game_id. Every game also carries "signals" (fleet.sim.signals;
the csv adapter derives the quarterback flags from the raw starter ids) and
"team_stats" ({"home": [...], "away": [...]}: that team's team_game_stats rows with a
strictly earlier kickoff, oldest first, at most TEAM_STATS_MAX) from the cache's
top-level "team_game_stats" list (empty lists without it).
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_left
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Iterable

from fleet.sim.signals import normalise_signals, qb_changed_map

TEAM_ALIASES = {"OAK": "LV", "SD": "LAC", "STL": "LA"}

FIELDS = (
    "game_id", "season", "game_type", "week", "kickoff_at", "home_team", "away_team",
    "home_score", "away_score", "home_moneyline", "away_moneyline", "spread_line",
    "total_line", "home_rest", "away_rest", "div_game", "roof", "surface", "temp", "wind",
    "signals",
)

FEATURE_KEYS = ("home_rest", "away_rest", "div_game", "roof", "surface", "temp", "wind",
                "week", "season", "game_type")
TEAM_STATS_MAX = 16
STAT_FLOATS = ("off_epa_per_play", "def_epa_per_play", "pass_rate", "success_rate")


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    return int(round(float(value)))


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalise_team(code: str) -> str:
    code = str(code).strip().upper()
    return TEAM_ALIASES.get(code, code)


def normalise_game_type(value: Any) -> str:
    text = str(value or "REG").strip().upper()
    return "REG" if text == "REG" else "POST"


def _eastern() -> tzinfo:
    """nflverse gameday/gametime are US Eastern; fall back to a fixed offset without tzdata."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # pragma: no cover - only without tzdata
        return timezone(timedelta(hours=-5))


def _kickoff_from_csv(gameday: str, gametime: str, tz: tzinfo) -> str:
    time_part = (gametime or "").strip() or "00:00"
    local = datetime.strptime(f"{gameday.strip()} {time_part}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    return local.astimezone(timezone.utc).isoformat()


def _normalise_kickoff(value: Any, game_id: str) -> str:
    """A fixed-format UTC ISO string so lexicographic order is chronological order."""
    if value is None or str(value).strip() == "":
        raise ValueError(f"game {game_id!r} has no kickoff_at")
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    """One JSON cache row (already in the MODELS.md field set) to the sim's dict shape."""
    game_id = str(row["game_id"])
    return {
        "game_id": game_id,
        "season": int(row["season"]),
        "game_type": normalise_game_type(row.get("game_type")),
        "week": int(row["week"]),
        "kickoff_at": _normalise_kickoff(row.get("kickoff_at"), game_id),
        "home_team": normalise_team(row["home_team"]),
        "away_team": normalise_team(row["away_team"]),
        "home_score": _int_or_none(row.get("home_score")),
        "away_score": _int_or_none(row.get("away_score")),
        "home_moneyline": _int_or_none(row.get("home_moneyline")),
        "away_moneyline": _int_or_none(row.get("away_moneyline")),
        "spread_line": _float_or_none(row.get("spread_line")),
        "total_line": _float_or_none(row.get("total_line")),
        "home_rest": _int_or_none(row.get("home_rest")),
        "away_rest": _int_or_none(row.get("away_rest")),
        "div_game": _int_or_none(row.get("div_game")) or 0,
        "roof": _str_or_none(row.get("roof")),
        "surface": _str_or_none(row.get("surface")),
        "temp": _int_or_none(row.get("temp")),
        "wind": _int_or_none(row.get("wind")),
        "signals": normalise_signals(row.get("signals")),
    }


def rows_from_csv(rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Adapt raw nflverse games.csv rows: kickoff from gameday + gametime (Eastern)."""
    tz = _eastern()
    out: list[dict[str, Any]] = []
    starters: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        row["kickoff_at"] = _kickoff_from_csv(row["gameday"], row.get("gametime", ""), tz)
        game = normalise_row(row)
        out.append(game)
        starters.append({key: game[key] for key in ("game_id", "kickoff_at", "home_team", "away_team")}
                        | {"home_qb_id": row.get("home_qb_id"), "away_qb_id": row.get("away_qb_id")})
    flags = qb_changed_map(starters)
    for game in out:
        game["signals"].update(flags[game["game_id"]])
    return out


def normalise_stat_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """One team_game_stats row in the contract's shape; None when it cannot be placed
    in time or has no EPA."""
    try:
        out = {
            "game_id": str(row["game_id"]),
            "season": int(row["season"]),
            "week": int(row["week"]),
            "team": normalise_team(row["team"]),
            "kickoff_at": _normalise_kickoff(row.get("kickoff_at"), str(row["game_id"])),
            "plays": _int_or_none(row.get("plays")),
        }
        for key in STAT_FLOATS:
            out[key] = _float_or_none(row.get(key))
    except (KeyError, TypeError, ValueError):
        return None
    if out["off_epa_per_play"] is None or out["def_epa_per_play"] is None:
        return None
    return out


def attach_team_stats(games: list[dict[str, Any]], stats: Iterable[dict[str, Any]]) -> None:
    """Set game["team_stats"] on every game (in place) from raw team_game_stats rows."""
    by_team: dict[str, list[dict[str, Any]]] = {}
    for raw in stats:
        row = normalise_stat_row(raw) if isinstance(raw, dict) else None
        if row is not None:
            by_team.setdefault(row["team"], []).append(row)
    keys: dict[str, list[str]] = {}
    for team, rows in by_team.items():
        rows.sort(key=lambda r: (r["kickoff_at"], r["game_id"]))
        keys[team] = [r["kickoff_at"] for r in rows]
    for game in games:
        attached: dict[str, list[dict[str, Any]]] = {}
        for side in ("home", "away"):
            team = game[f"{side}_team"]
            rows = by_team.get(team, [])
            end = bisect_left(keys.get(team, []), game["kickoff_at"])
            attached[side] = rows[max(0, end - TEAM_STATS_MAX):end]
        game["team_stats"] = attached


def sort_games(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(games, key=lambda g: (g["kickoff_at"], g["game_id"]))


def load_games(path: str) -> list[dict[str, Any]]:
    """Games from the agent's JSON cache (a list, or an object holding one) or a games.csv."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        head = fh.read(1)
        fh.seek(0)
        stats: list[Any] = []
        if head == "g":  # a csv header line starts with game_id
            games = rows_from_csv(csv.DictReader(fh))
        else:
            payload = json.load(fh)
            if isinstance(payload, dict):
                stats = payload.get("team_game_stats") or []
                payload = payload.get("games") or payload.get("rows") or []
            games = [normalise_row(row) for row in payload]
    games = sort_games(games)
    attach_team_stats(games, stats if isinstance(stats, list) else [])
    return games


def _team_stats_of(value: Any) -> dict[str, list[dict[str, Any]]]:
    value = value if isinstance(value, dict) else {}
    out: dict[str, list[dict[str, Any]]] = {}
    for side in ("home", "away"):
        rows = value.get(side)
        out[side] = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    return out


def features_of(game: dict[str, Any]) -> dict[str, Any]:
    """The features dict handed to Model.predict: FEATURE_KEYS plus "signals" (all
    zeros when absent) and "team_stats" (empty lists when absent)."""
    features = {key: game.get(key) for key in FEATURE_KEYS}
    features["signals"] = normalise_signals(game.get("signals"))
    features["team_stats"] = _team_stats_of(game.get("team_stats"))
    return features


def game_key(game: dict[str, Any]) -> tuple[int, int]:
    return (game["season"], game["week"])


def has_moneylines(game: dict[str, Any]) -> bool:
    return game["home_moneyline"] is not None and game["away_moneyline"] is not None


def outcome_of(game: dict[str, Any]) -> float | None:
    """1 for a home win, 0.5 for a tie, 0 for an away win, None when unplayed."""
    hs, as_ = game["home_score"], game["away_score"]
    if hs is None or as_ is None:
        return None
    if hs > as_:
        return 1.0
    if hs < as_:
        return 0.0
    return 0.5


def complete_seasons(games: list[dict[str, Any]]) -> list[int]:
    """Seasons in which every game has a score, ascending."""
    seen: dict[int, bool] = {}
    for g in games:
        complete = outcome_of(g) is not None
        seen[g["season"]] = seen.get(g["season"], True) and complete
    return sorted(s for s, ok in seen.items() if ok)
