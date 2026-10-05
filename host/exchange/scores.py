"""Finals from the ESPN scoreboard (docs/TRADING.md, "Settlement").

On game days the exchange polls `settings.scores_url` every 60 s while a game with an
assignment has kicked off and is not final. A completed event maps to a game by its
two teams and date (through the alias table) and sets the scores, `status final` and
`raw.score_source = "espn"`; the nflverse refresh confirms later. Parsing is
fixture-driven and defensive: an event without teams, scores or a completed status
changes nothing.

In the exchange the fetch goes through the game-state feed's ESPN budget
(host/exchange/gamestate.py `scores_fetch`): one sliding window and one 429/403
backoff for every ESPN request. A fetch that may not go out now raises Deferred and
the pass reports `deferred` instead of an error (the exchange asks again a second
later, after the feed's next pass asked the scoreboard).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from host.exchange.adapters import teams
from host.exchange.adapters.base import http_get, parse_time, truncate, utcnow
from host.nflverse import EASTERN
from host.settings import get_setting

log = logging.getLogger(__name__)

DEFAULT_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
WINDOW = timedelta(hours=36)
MAX_SCORE = 999


class Deferred(Exception):
    """The fetch may not go out now (ESPN backoff or rate window); nothing failed."""


def _int(value: Any) -> int | None:
    """A score: a whole number 0..999, else None (an event without it changes nothing)."""
    if isinstance(value, bool):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= MAX_SCORE else None


def _competitor(entry: dict[str, Any]) -> tuple[str | None, int | None]:
    team = entry.get("team") if isinstance(entry.get("team"), dict) else {}
    code = None
    for key in ("abbreviation", "displayName", "name", "location", "shortDisplayName"):
        code = teams.resolve(team.get(key)) if isinstance(team.get(key), str) else None
        if code:
            break
    return code, _int(entry.get("score"))


def parse_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """{"event_id", "date", "completed", "home", "away", "home_score", "away_score"}
    for one scoreboard event; None when it has no identifiable teams."""
    competitions = event.get("competitions")
    competition = competitions[0] if isinstance(competitions, list) and competitions and isinstance(competitions[0], dict) else {}
    competitors = competition.get("competitors") if isinstance(competition.get("competitors"), list) else []
    home = away = None
    home_score = away_score = None
    for entry in competitors:
        if not isinstance(entry, dict):
            continue
        code, score = _competitor(entry)
        if entry.get("homeAway") == "home":
            home, home_score = code, score
        elif entry.get("homeAway") == "away":
            away, away_score = code, score
    if home is None or away is None:
        log.debug("espn event without two teams: %s", truncate(json.dumps(event), 2048))
        return None
    status = competition.get("status") if isinstance(competition.get("status"), dict) else event.get("status")
    status = status if isinstance(status, dict) else {}
    kind = status.get("type") if isinstance(status.get("type"), dict) else {}
    completed = kind.get("completed") is True or str(kind.get("name") or "").upper() == "STATUS_FINAL"
    return {
        "event_id": str(event.get("id")) if event.get("id") is not None else None,
        "date": parse_time(competition.get("date") or event.get("date")),
        "completed": completed,
        "home": home,
        "away": away,
        "home_score": home_score,
        "away_score": away_score,
    }


def parse_scoreboard(payload: Any) -> list[dict[str, Any]]:
    """Every parseable event of a scoreboard payload (JSON text or object)."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except ValueError:
            log.debug("espn scoreboard is not JSON: %s", truncate(str(payload), 2048))
            return []
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        log.debug("espn scoreboard without events: %s", truncate(json.dumps(payload, default=str), 2048))
        return []
    out = []
    for event in events:
        parsed = parse_event(event) if isinstance(event, dict) else None
        if parsed is not None:
            out.append(parsed)
    return out


def match_game(conn: psycopg.Connection, entry: dict[str, Any]) -> dict[str, Any] | None:
    """The game with the entry's teams on its date (or kickoff within 36 h)."""
    when = entry.get("date")
    if when is None:
        return None
    gameday = when.astimezone(EASTERN).date()
    rows = conn.execute(
        """
        SELECT * FROM games
         WHERE home_team = %(h)s AND away_team = %(a)s
           AND (gameday = %(day)s OR kickoff_at BETWEEN %(lo)s AND %(hi)s)
         ORDER BY (gameday = %(day)s) DESC, kickoff_at
        """,
        {"h": entry["home"], "a": entry["away"], "day": gameday, "lo": when - WINDOW, "hi": when + WINDOW},
    ).fetchall()
    return dict(rows[0]) if rows else None


def apply(conn: psycopg.Connection, entries: list[dict[str, Any]]) -> list[str]:
    """Write finals for completed entries; the game ids that changed."""
    changed: list[str] = []
    for entry in entries:
        if not entry.get("completed") or entry.get("home_score") is None or entry.get("away_score") is None:
            continue
        game = match_game(conn, entry)
        if game is None:
            continue
        same = (
            game["status"] == "final"
            and game["home_score"] == entry["home_score"]
            and game["away_score"] == entry["away_score"]
        )
        if same:
            continue
        raw = dict(game["raw"] or {})
        raw["score_source"] = "espn"
        conn.execute(
            """
            UPDATE games SET home_score = %s, away_score = %s, status = 'final', raw = %s,
                   updated_at = clock_timestamp()
             WHERE game_id = %s
            """,
            (entry["home_score"], entry["away_score"], Jsonb(raw), game["game_id"]),
        )
        changed.append(game["game_id"])
    return changed


def games_awaiting_scores(conn: psycopg.Connection, now: datetime) -> list[str]:
    """Games with assignments that kicked off and are not final yet."""
    rows = conn.execute(
        """
        SELECT DISTINCT g.game_id FROM games g JOIN assignments a ON a.game_id = g.game_id
         WHERE g.status <> 'final' AND g.kickoff_at <= %s AND a.status IN ('active', 'halted')
        """,
        (now,),
    ).fetchall()
    return [r["game_id"] for r in rows]


def fetch_scoreboard(url: str, timeout: float = 15.0) -> str:
    status, text = http_get(url, timeout)
    if status != 200:
        raise RuntimeError(f"scoreboard answered {status}: {truncate(text, 256)}")
    return text


def poll(conn: psycopg.Connection, now: datetime | None = None, fetch: Any = None) -> dict[str, Any]:
    """Fetch and apply finals when a game with assignments awaits a score; with
    "deferred" (the reason) when the fetch may not go out now."""
    now = now or utcnow()
    waiting = games_awaiting_scores(conn, now)
    if not waiting:
        return {"waiting": 0, "changed": []}
    url = str(get_setting(conn, "scores_url", DEFAULT_URL) or DEFAULT_URL)
    try:
        text = (fetch or fetch_scoreboard)(url)
    except Deferred as exc:
        return {"waiting": len(waiting), "changed": [], "deferred": str(exc)}
    changed = apply(conn, parse_scoreboard(text))
    return {"waiting": len(waiting), "changed": changed}
