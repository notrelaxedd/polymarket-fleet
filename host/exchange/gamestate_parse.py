"""Parsers for the live game-state feed (docs/INGAME.md, "Live game state").

Every parser takes JSON text or a decoded object and never raises: anything it cannot
read yields an empty result. A state dict has exactly the keys in STATE_KEYS;
`yardline_100` is the distance to the opponent's end zone for the team in possession
(ESPN's `yardsToEndzone`, nflverse's `yardline_100`).

ESPN summary (`.../summary?event=<id>`), the shapes this module reads (the fixtures in
tests/fixtures/espn_summary_*.json, written from ESPN's documented payloads and not yet
verified against a live game):

- header.competitions[0].status {period, clock (seconds), displayClock, type {state
  "pre" | "in" | "post", completed, name STATUS_HALFTIME | STATUS_END_PERIOD | ...}}
- header.competitions[0].competitors[] {homeAway, score, team {id, abbreviation}}
- situation {down, distance, possession (team id), possessionText ("LV 38"),
  yardsToEndzone (when present), homeTimeouts, awayTimeouts}; `yardLine` alone is not
  used because its orientation is unverified
- drives.previous[-1] and drives.current: plays[] {id, text, clock.displayValue,
  period.number, homeScore, awayScore (after the play), start {down, distance,
  yardsToEndzone, team.id}, wallclock}

A play becomes the state at its snap (the nflverse convention the model trains on):
the situation from `start`, the score before the play (the previous play's score
after). The current situation comes last, built from the header and `situation`.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from host.exchange.adapters import teams
from host.exchange.adapters.base import parse_time, truncate

log = logging.getLogger(__name__)

STATE_KEYS = ("status", "period", "clock_seconds", "home_score", "away_score", "possession", "down", "distance",
              "yardline_100", "home_timeouts", "away_timeouts")
CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?:\.\d+)?\s*$")
SPOT_RE = re.compile(r"^\s*([A-Za-z]{2,4})?\s*(\d{1,2})\s*$")
OFF_NAMES = ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_FORFEIT")
MAX_SCORE = 999
NUL_ESCAPE_RE = re.compile(r"(?<!\\)((?:\\\\)*)\\u0000")


def _int(value: Any, lo: int | None = None, hi: int | None = None) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if (lo is not None and number < lo) or (hi is not None and number > hi):
        return None
    return number


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def load(payload: Any) -> Any:
    """Decode JSON text; None when it is not JSON."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            log.debug("game-state payload is not JSON: %s", truncate(payload, 512))
            return None
    return payload


def clock_seconds(value: Any) -> int | None:
    """"8:32" (or a number of seconds) to whole seconds left in the period."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _int(value, 0, 3600)
    match = CLOCK_RE.match(str(value or ""))
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def status_of(status: dict[str, Any]) -> str | None:
    """ESPN status block to "pre" | "in" | "half" | "end_period" | "final"; a postponed,
    cancelled or forfeited game is final for the feed. Without type.state only a name
    that says what the game is doing counts: any other STATUS_* name is None."""
    kind = _dict(status.get("type"))
    name = str(kind.get("name") or "").upper()
    state = str(kind.get("state") or "").lower()
    if kind.get("completed") is True or name.startswith("STATUS_FINAL") or name in OFF_NAMES or state == "post":
        return "final"
    if state == "pre" or name == "STATUS_SCHEDULED":
        return "pre"
    if name == "STATUS_HALFTIME" or str(kind.get("description") or "").lower() == "halftime":
        return "half"
    if name in ("STATUS_END_PERIOD", "STATUS_END_OF_PERIOD"):
        return "end_period"
    return "in" if state == "in" or name == "STATUS_IN_PROGRESS" else None


def sides(competition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{"home": {"ids", "code", "score"}, "away": {...}} from the competitors."""
    out: dict[str, dict[str, Any]] = {}
    for entry in _list(competition.get("competitors")):
        entry = _dict(entry)
        side = entry.get("homeAway")
        if side not in ("home", "away"):
            continue
        team = _dict(entry.get("team"))
        ids = {str(v) for v in (team.get("id"), entry.get("id")) if v is not None}
        code = teams.resolve(team.get("abbreviation")) if isinstance(team.get("abbreviation"), str) else None
        out[side] = {"ids": ids, "code": code, "score": _int(entry.get("score"), 0, MAX_SCORE)}
    return out if len(out) == 2 else {}


def side_of(team_id: Any, sides_: dict[str, dict[str, Any]]) -> str | None:
    if team_id is None:
        return None
    key = str(_dict(team_id).get("id")) if isinstance(team_id, dict) else str(team_id)
    for side, info in sides_.items():
        if key in info["ids"]:
            return side
    return None


def yardline_from_text(text: Any, possession: str | None, sides_: dict[str, dict[str, Any]]) -> int | None:
    """"LV 38" with KC in possession -> 38; "KC 25" -> 75; "50" -> 50."""
    match = SPOT_RE.match(str(text or ""))
    if not match or possession is None:
        return None
    spot = int(match.group(2))
    if spot == 50:
        return 50
    if not match.group(1) or not 1 <= spot < 50:
        return None
    own = sides_.get(possession, {}).get("code")
    half = teams.resolve(match.group(1))
    if own is None or half is None:
        return None
    return 100 - spot if half == own else spot


def blank_state(status: str) -> dict[str, Any]:
    state: dict[str, Any] = dict.fromkeys(STATE_KEYS)
    state["status"] = status
    return state


def situation_state(status: str, status_block: dict[str, Any], situation: dict[str, Any],
                    sides_: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The current situation from a status block, the competitors and `situation`."""
    state = blank_state(status)
    state["period"] = _int(status_block.get("period"), 1, 20)
    clock = status_block.get("clock")
    state["clock_seconds"] = clock_seconds(clock if isinstance(clock, (int, float)) else status_block.get("displayClock"))
    state["home_score"] = sides_["home"]["score"]
    state["away_score"] = sides_["away"]["score"]
    if status == "pre":
        state["home_score"] = state["home_score"] or 0
        state["away_score"] = state["away_score"] or 0
        state["period"] = state["clock_seconds"] = None
    if status in ("in", "half", "end_period") and situation:
        possession = side_of(situation.get("possession"), sides_)
        state["possession"] = possession
        if possession is not None:
            state["down"] = _int(situation.get("down"), 1, 4)
            state["distance"] = _int(situation.get("distance"), 0, 99) if state["down"] else None
            state["yardline_100"] = (_int(situation.get("yardsToEndzone"), 1, 99)
                                     or yardline_from_text(situation.get("possessionText"), possession, sides_))
        state["home_timeouts"] = _int(situation.get("homeTimeouts"), 0, 3)
        state["away_timeouts"] = _int(situation.get("awayTimeouts"), 0, 3)
    return state


def play_rows(drives: dict[str, Any], sides_: dict[str, dict[str, Any]], timeouts: tuple[Any, Any]) -> list[dict[str, Any]]:
    """The plays of the last finished drive and the current drive, in order, as
    states at the snap with play_id, play_text, event_ts and raw."""
    plays: list[dict[str, Any]] = []
    previous = _list(drives.get("previous"))
    for drive in ([previous[-1]] if previous else []) + [drives.get("current")]:
        plays.extend(_dict(p) for p in _list(_dict(drive).get("plays")))
    out: list[dict[str, Any]] = []
    before: tuple[int | None, int | None] | None = None
    seen: set[str] = set()
    for play in plays:
        play_id = play.get("id")
        after = (_int(play.get("homeScore"), 0, MAX_SCORE), _int(play.get("awayScore"), 0, MAX_SCORE))
        if play_id is None or str(play_id) in seen:
            continue
        seen.add(str(play_id))
        start = _dict(play.get("start"))
        state = blank_state("in")
        state["period"] = _int(_dict(play.get("period")).get("number"), 1, 20)
        clock = _dict(play.get("clock"))
        state["clock_seconds"] = clock_seconds(clock.get("displayValue") if clock.get("displayValue") else clock.get("value"))
        state["home_score"], state["away_score"] = before if before is not None else after
        possession = side_of(start.get("team"), sides_)
        state["possession"] = possession
        if possession is not None:
            state["down"] = _int(start.get("down"), 1, 4)
            state["distance"] = _int(start.get("distance"), 0, 99) if state["down"] else None
            state["yardline_100"] = _int(start.get("yardsToEndzone"), 1, 99)
        state["home_timeouts"], state["away_timeouts"] = timeouts
        before = after
        text = play.get("text")
        out.append({**state, "play_id": _text(play_id), "play_text": _text(text)[:1000] if text is not None else None,
                    "event_ts": parse_time(play.get("wallclock")), "raw": play})
    return out


def parse_summary_rows(payload: Any) -> list[dict[str, Any]]:
    """parse_summary with each element's source fragment under "raw" (for storage)."""
    try:
        return _summary(load(payload))
    except Exception as exc:  # noqa: BLE001 - a parser never raises
        log.warning("espn summary not parsed: %s", exc)
        return []


def _summary(data: Any) -> list[dict[str, Any]]:
    header = _dict(_dict(data).get("header"))
    competition = _dict(next(iter(_list(header.get("competitions"))), None))
    status_block = _dict(competition.get("status")) or _dict(header.get("status"))
    sides_ = sides(competition)
    status = status_of(status_block)
    if not sides_ or status is None:
        return []
    situation = _dict(_dict(data).get("situation")) or _dict(competition.get("situation"))
    current = situation_state(status, status_block, situation, sides_)
    plays = play_rows(_dict(_dict(data).get("drives")), sides_, (current["home_timeouts"], current["away_timeouts"]))
    last_ts: datetime | None = plays[-1]["event_ts"] if plays else None
    raw = {"status": status_block, "situation": situation or None}
    return plays + [{**current, "play_id": None, "play_text": None, "event_ts": last_ts, "raw": raw}]


def parse_summary(payload: Any) -> list[dict[str, Any]]:
    """State dicts plus "play_id", "play_text" and "event_ts", one per play of the last
    finished and the current drive, with the current situation as the last element;
    empty on anything unparseable. Never raises."""
    return [{k: v for k, v in row.items() if k != "raw"} for row in parse_summary_rows(payload)]


def parse_scoreboard_states(payload: Any) -> dict[str, dict[str, Any]]:
    """{event_id: state dict} from the scoreboard (the fallback while the summary has
    no plays yet). Events without two teams or a status are left out. Never raises."""
    out: dict[str, dict[str, Any]] = {}
    try:
        for event in _list(_dict(load(payload)).get("events")):
            event = _dict(event)
            competition = _dict(next(iter(_list(event.get("competitions"))), None))
            status_block = _dict(competition.get("status")) or _dict(event.get("status"))
            sides_ = sides(competition)
            status = status_of(status_block)
            if event.get("id") is None or not sides_ or status is None:
                continue
            out[str(event["id"])] = situation_state(status, status_block, _dict(competition.get("situation")), sides_)
    except Exception as exc:  # noqa: BLE001 - a parser never raises
        log.warning("espn scoreboard states not parsed: %s", exc)
    return out


def _text(value: Any) -> str:
    """Text for a Postgres text column, which cannot hold NUL."""
    return str(value).replace("\x00", "")


def raw_fragment(value: Any, limit: int = 8 * 1024) -> Any:
    """The source fragment for game_state.raw: as is when its JSON fits in `limit`
    characters (NUL characters dropped: jsonb refuses them), else {"truncated": true,
    "text": the first `limit` characters}; a fragment with NaN or Infinity, which JSON
    lacks, is kept as text too."""
    try:
        text = json.dumps(value, default=str, allow_nan=False)
    except ValueError:
        text = json.dumps(value, default=str)
        return {"truncated": True, "text": text[:limit]}
    if len(text) > limit:
        return {"truncated": True, "text": text[:limit]}
    return json.loads(NUL_ESCAPE_RE.sub(r"\1", text)) if "\\u0000" in text else value
