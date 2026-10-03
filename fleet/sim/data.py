"""Load games for the sim: the agent's JSON cache (GET /api/v1/data/games) or the raw
nflverse games.csv (tests load the fixture directly through the csv adapter).

Every row becomes a plain dict with the fields listed in docs/MODELS.md, team codes
normalised for continuity, season and week as ints, scores and moneylines as ints or
None, sorted by kickoff then game_id.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Iterable

TEAM_ALIASES = {"OAK": "LV", "SD": "LAC", "STL": "LA"}

FIELDS = (
    "game_id", "season", "game_type", "week", "kickoff_at", "home_team", "away_team",
    "home_score", "away_score", "home_moneyline", "away_moneyline", "spread_line",
    "total_line", "home_rest", "away_rest", "div_game", "roof", "surface", "temp", "wind",
)

FEATURE_KEYS = ("home_rest", "away_rest", "div_game", "roof", "surface", "temp", "wind",
                "week", "season", "game_type")


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
    }


def rows_from_csv(rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Adapt raw nflverse games.csv rows: kickoff from gameday + gametime (Eastern)."""
    tz = _eastern()
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        row["kickoff_at"] = _kickoff_from_csv(row["gameday"], row.get("gametime", ""), tz)
        out.append(normalise_row(row))
    return out


def sort_games(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(games, key=lambda g: (g["kickoff_at"], g["game_id"]))


def load_games(path: str) -> list[dict[str, Any]]:
    """Games from the agent's JSON cache (a list, or an object holding one) or a games.csv."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        head = fh.read(1)
        fh.seek(0)
        if head == "g":  # a csv header line starts with game_id
            games = rows_from_csv(csv.DictReader(fh))
        else:
            payload = json.load(fh)
            if isinstance(payload, dict):
                payload = payload.get("games") or payload.get("rows") or []
            games = [normalise_row(row) for row in payload]
    return sort_games(games)


def features_of(game: dict[str, Any]) -> dict[str, Any]:
    """The features dict handed to Model.predict."""
    return {key: game.get(key) for key in FEATURE_KEYS}


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
